"""
Copyright (C) 2019 NVIDIA Corporation.  All rights reserved.
Licensed under the CC BY-NC-SA 4.0 license
(https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
"""
import torch
import torch.nn.functional as F
from torch import nn
import math
class EqualLinear(nn.Module):
    def __init__(
            self, in_dim, out_dim, bias=True, bias_init=0, lr_mul=1, activation=None
    ):
        super().__init__()
        self.weight = nn.Parameter(torch.randn(out_dim, in_dim).div_(lr_mul))
        self.bias = nn.Parameter(torch.zeros(out_dim).fill_(bias_init))
        self.activation = activation
        self.scale = (1 / math.sqrt(in_dim)) * lr_mul
        self.lr_mul = lr_mul

    def forward(self, input):
        out = F.linear(input, self.weight * self.scale, bias=self.bias * self.lr_mul)
        return out

    def __repr__(self):
        return (
            f'{self.__class__.__name__}({self.weight.shape[1]}, {self.weight.shape[0]})'
        )


class ModulatedConv2d(nn.Module):
    def __init__(self, in_dim, out_dim, ks, st, padding=0,
                 norm='none', act='relu', pad_type='zero',
                 use_bias=True, use_sn=False):
        super(ModulatedConv2d, self).__init__()
        self.use_bias = use_bias
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.ks = ks
        # initialize padding
        if pad_type == 'reflect':
            self.pad = nn.ReflectionPad2d(padding)
        elif pad_type == 'replicate':
            self.pad = nn.ReplicationPad2d(padding)
        elif pad_type == 'zero':
            self.pad = nn.ZeroPad2d(padding)
        else:
            assert 0, "Unsupported padding type: {}".format(pad_type)

        # initialize activation
        if act == 'relu':
            self.activation = nn.ReLU(inplace=True)
        elif act == 'lrelu':
            self.activation = nn.LeakyReLU(0.2, inplace=True)
        elif act == 'tanh':
            self.activation = nn.Tanh()
        elif act == 'none':
            self.activation = None
        else:
            assert 0, "Unsupported activation: {}".format(act)

        fan_in = in_dim * ks ** 2
        self.scale = 1 / math.sqrt(fan_in)
        self.weight = nn.Parameter(
            torch.randn(1, out_dim, in_dim, ks, ks)
        )
        self.modulation = EqualLinear(4096, in_dim, bias_init=1)



    def forward(self, input, s):
        input = self.pad(input)

        ################################################
        batch, in_channel, height, width = input.shape
        style = self.modulation(s).view(batch, 1, in_channel, 1, 1)
        weight = self.scale * self.weight * style
        demod = torch.rsqrt(weight.pow(2).sum([2, 3, 4]) + 1e-8)
        weight = weight * demod.view(batch, self.out_dim, 1, 1, 1)
        weight = weight.view(batch * self.out_dim, in_channel, self.ks, self.ks)

        input = input.view(1, batch * in_channel, height, width)
        out = F.conv2d(input, weight, padding=0, groups=batch)
        _, _, height, width = out.shape
        out = out.view(batch, self.out_dim, height, width)
        ################################################

        if self.activation:
            out = self.activation(out)
        return out
class ModResBlocks(nn.Module):
    def __init__(self, num_blocks, dim, norm, act, pad_type, use_sn=False):
        super(ModResBlocks, self).__init__()
        self.model = nn.ModuleList()

        self.m1 = ModResBlock(dim, norm=norm, act=act, pad_type=pad_type, use_sn=use_sn)

        self.m2 = ModResBlock(dim, norm=norm, act=act, pad_type=pad_type, use_sn=use_sn)
    def forward(self, x, s):
        return self.m2(self.m1(x, s), s)

class ResBlocks(nn.Module):
    def __init__(self, num_blocks, dim, norm, act, pad_type, use_sn=False):
        super(ResBlocks, self).__init__()
        self.model = nn.ModuleList()
        for i in range(num_blocks):
            self.model.append(ResBlock(dim, norm=norm, act=act, pad_type=pad_type, use_sn=use_sn))
        self.model = nn.Sequential(*self.model)

    def forward(self, x):
        return self.model(x)
class ModResBlock(nn.Module):
    def __init__(self, dim, norm='in', act='relu', pad_type='zero', use_sn=False):
        super(ModResBlock, self).__init__()
        self.mm1 = ModulatedConv2d(dim, dim, 3, 1, 1,
                                               norm=norm,
                                               act=act,
                                               pad_type=pad_type, use_sn=use_sn)
        self.mm2 = ModulatedConv2d(dim, dim, 3, 1, 1,
                                               norm=norm,
                                               act='none',
                                               pad_type=pad_type, use_sn=use_sn)

    def forward(self, x, s):
        x_org = x
        residual = self.mm2(self.mm1(x, s), s)
        out = x_org + 0.1 * residual
        return out

class ResBlock(nn.Module):
    def __init__(self, dim, norm='in', act='relu', pad_type='zero', use_sn=False):
        super(ResBlock, self).__init__()
        self.model = nn.Sequential(Conv2dBlock(dim, dim, 3, 1, 1,
                                               norm=norm,
                                               act=act,
                                               pad_type=pad_type, use_sn=use_sn),
                                   Conv2dBlock(dim, dim, 3, 1, 1,
                                               norm=norm,
                                               act='none',
                                               pad_type=pad_type, use_sn=use_sn))

    def forward(self, x):
        x_org = x
        residual = self.model(x)
        out = x_org + 0.1 * residual
        return out


class ActFirstResBlk(nn.Module):
    def __init__(self, dim_in, dim_out, downsample=True):
        super(ActFirstResBlk, self).__init__()
        self.norm1 = FRN(dim_in)
        self.norm2 = FRN(dim_in)
        self.conv1 = nn.Conv2d(dim_in, dim_in, 3, 1, 1)
        self.conv2 = nn.Conv2d(dim_in, dim_out, 3, 1, 1)
        self.downsample = downsample
        self.learned_sc = (dim_in != dim_out)
        if self.learned_sc:
            self.conv1x1 = nn.Conv2d(dim_in, dim_out, 1, 1, 0, bias=False)

    def _shortcut(self, x):
        if self.learned_sc:
            x = self.conv1x1(x)
        if self.downsample:
            x = F.avg_pool2d(x, 2)
        return x

    def _residual(self, x):
        x = self.norm1(x)
        x = self.conv1(x)
        if self.downsample:
            x = F.avg_pool2d(x, 2)
        x = self.norm2(x)
        x = self.conv2(x)
        return x

    def forward(self, x):
        return torch.rsqrt(torch.tensor(2.0)) * self._shortcut(x) + torch.rsqrt(torch.tensor(2.0)) * self._residual(x)

class SelfAttention(nn.Module):
    """ Self attention Layer"""

    def __init__(self, in_dim, activation=None):
        super(SelfAttention, self).__init__()
        self.chanel_in = in_dim
        self.activation = activation

        self.query_conv = nn.Conv2d(
            in_channels=in_dim, out_channels=in_dim//8, kernel_size=1)
        self.key_conv = nn.Conv2d(
            in_channels=in_dim, out_channels=in_dim//8, kernel_size=1)
        self.value_conv = nn.Conv2d(
            in_channels=in_dim, out_channels=in_dim, kernel_size=1)

        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, y):
        """
            inputs :
                x : input feature maps( B X C X W X H)
            returns :
                out : self attention value + input feature
                attention: B X N X N (N is Width*Height)
        """
        # print('attention size', x.size())
        # print('attention size', x.size())
        m_batchsize, C, width, height = x.size()
        # print('query_conv size', self.query_conv(x).size())
        proj_query = self.query_conv(x).view(m_batchsize, -1, width * height).permute(0, 2, 1)  # 8 X 16 X 64
        proj_key = self.key_conv(y).view(m_batchsize, -1, width * height)  # B X C X (W*H)
        energy = torch.bmm(proj_query, proj_key)  # transpose check
        attention = self.softmax(energy)  # B X (N) X (N)
        proj_value = self.value_conv(y).view(m_batchsize, -1, width * height)  # B X C X N

        out = torch.bmm(proj_value, attention.permute(0, 2, 1))
        out = out.view(m_batchsize, C, width, height)

        out = 0.1 * out + x
        return out
class LinearBlock(nn.Module):
    def __init__(self, in_dim, out_dim, norm='none', act='relu', use_sn=False):
        super(LinearBlock, self).__init__()
        use_bias = True
        self.fc = nn.Linear(in_dim, out_dim, bias=use_bias)
        if use_sn:
            self.fc = nn.utils.spectral_norm(self.fc)

        # initialize normalization
        norm_dim = out_dim
        if norm == 'bn':
            self.norm = nn.BatchNorm1d(norm_dim)
        elif norm == 'in':
            self.norm = nn.InstanceNorm1d(norm_dim)
        elif norm == 'none':
            self.norm = None
        else:
            assert 0, "Unsupported normalization: {}".format(norm)

        # initialize activation
        if act == 'relu':
            self.activation = nn.ReLU(inplace=True)
        elif act == 'lrelu':
            self.activation = nn.LeakyReLU(0.2, inplace=True)
        elif act == 'tanh':
            self.activation = nn.Tanh()
        elif act == 'none':
            self.activation = None
        else:
            assert 0, "Unsupported activation: {}".format(act)

    def forward(self, x):
        out = self.fc(x)
        if self.norm:
            out = self.norm(out)
        if self.activation:
            out = self.activation(out)
        return out


class Conv2dBlock(nn.Module):
    def __init__(self, in_dim, out_dim, ks, st, padding=0,
                 norm='none', act='relu', pad_type='zero',
                 use_bias=True, use_sn=False):
        super(Conv2dBlock, self).__init__()
        self.use_bias = use_bias

        # initialize padding
        if pad_type == 'reflect':
            self.pad = nn.ReflectionPad2d(padding)
        elif pad_type == 'replicate':
            self.pad = nn.ReplicationPad2d(padding)
        elif pad_type == 'zero':
            self.pad = nn.ZeroPad2d(padding)
        else:
            assert 0, "Unsupported padding type: {}".format(pad_type)

        # initialize normalization
        norm_dim = out_dim
        if norm == 'bn':
            self.norm = nn.BatchNorm2d(norm_dim)
        elif norm == 'in':
            self.norm = nn.InstanceNorm2d(norm_dim)
        elif norm == 'adain':
            self.norm = AdaIN2d(norm_dim)
        elif norm == 'none':
            self.norm = None
        else:
            assert 0, "Unsupported normalization: {}".format(norm)

        # initialize activation
        if act == 'relu':
            self.activation = nn.ReLU(inplace=True)
        elif act == 'lrelu':
            self.activation = nn.LeakyReLU(0.2, inplace=True)
        elif act == 'tanh':
            self.activation = nn.Tanh()
        elif act == 'none':
            self.activation = None
        else:
            assert 0, "Unsupported activation: {}".format(act)

        self.conv = nn.Conv2d(in_dim, out_dim, ks, st, bias=self.use_bias)
        if use_sn:
            self.conv = nn.utils.spectral_norm(self.conv)

    def forward(self, x):
        x = self.conv(self.pad(x))
        if self.norm:
            x = self.norm(x)
        if self.activation:
            x = self.activation(x)
        return x


class FRN(nn.Module):
    def __init__(self, num_features, eps=1e-6):
        super(FRN, self).__init__()
        self.tau = nn.Parameter(torch.zeros(1, num_features, 1, 1))
        self.gamma = nn.Parameter(torch.ones(1, num_features, 1, 1))
        self.beta = nn.Parameter(torch.zeros(1, num_features, 1, 1))
        self.eps = eps

    def forward(self, x):
        x = x * torch.rsqrt(torch.mean(x**2, dim=[2, 3], keepdim=True) + self.eps)
        return torch.max(self.gamma * x + self.beta, self.tau)


class AdaIN2d(nn.Module):
    def __init__(self, num_features, eps=1e-5, momentum=0.1, affine=False, track_running_stats=True):
        super(AdaIN2d, self).__init__()
        self.num_features = num_features
        self.eps = eps
        self.momentum = momentum
        self.affine = affine
        self.track_running_stats = track_running_stats

        if self.affine:
            self.weight = nn.Parameter(torch.Tensor(num_features))
            self.bias = nn.Parameter(torch.Tensor(num_features))
        else:
            self.weight = None
            self.bias = None

        if self.track_running_stats:
            self.register_buffer('running_mean', torch.zeros(num_features))
            self.register_buffer('running_var', torch.ones(num_features))
        else:
            self.register_buffer('running_mean', None)
            self.register_buffer('running_var', None)

    def forward(self, x):
        assert self.weight is not None and self.bias is not None, "AdaIN params are None"
        N, C, H, W = x.size()
        running_mean = self.running_mean.repeat(N)
        running_var = self.running_var.repeat(N)
        x_ = x.contiguous().view(1, N * C, H * W)
        normed = F.batch_norm(x_, running_mean, running_var,
                              self.weight, self.bias,
                              True, self.momentum, self.eps)
        return normed.view(N, C, H, W)

    def __repr__(self):
        return self.__class__.__name__ + '(num_features=' + str(self.num_features) + ')'


if __name__ == '__main__':
    print("CALL blocks_stylegan2.py")
