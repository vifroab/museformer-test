import torch

import triton
import triton.language as tl


def num_warps(n):
    if n < 512:
        return 4
    if n < 2048:
        return 8
    return 16


@triton.heuristics({'num_warps': lambda nargs: num_warps(nargs['sizemax'] * nargs['BLOCK'])})
@triton.heuristics({'TN': lambda nargs: triton.next_power_of_2(nargs['sizemax'] * nargs['BLOCK'])})
@triton.jit
def _forward(
    X, scale, LUT, RPE, KP_M, ATTN_M, is_causal, sizemax, stride_zx, stride_zrpe, stride_hrpe, stride_srpe, stride_zkpm, stride_zattnm,
    TN: tl.constexpr, BLOCK: tl.constexpr, APPLY_SCALE: tl.constexpr, APPLY_RPE: tl.constexpr, APPLY_KP_MASK: tl.constexpr,
    KP_MASK_MUL: tl.constexpr, APPLY_ATTN_MASK: tl.constexpr, ATTN_MASK_MUL: tl.constexpr,
):
    pidhm = tl.program_id(0)
    pidz = tl.program_id(1)
    # create index ranges
    rxm = pidhm % BLOCK
    rbm = pidhm // BLOCK
    rxn = tl.arange(0, TN) % BLOCK
    rbn = tl.arange(0, TN) // BLOCK
    # extract information from LUT
    header = LUT + rbm * 2
    size = tl.load(header + 0)
    offset = tl.load(header + 1)
    check = rbn < size
    rbmn = tl.where(check, rbn, size - 1)
    # block id and column id
    blockid = tl.load(LUT + offset + rbmn * 4 + 0)
    columnid = tl.load(LUT + offset + rbmn * 4 + 1)
    rowid = tl.load(LUT + offset + rbmn * 4 + 2)
    headid = tl.load(LUT + offset + rbmn * 4 + 3)
    # pointers to X
    px = X + pidz * stride_zx + blockid * BLOCK * BLOCK + rxm * BLOCK + rxn
    x = tl.load(px, mask=check, other=-float('inf'))
    x = x.to(tl.float32)
    # apply scale
    if APPLY_SCALE:
        x = x * scale
    # apply RPE
    if APPLY_RPE:
        prpe = RPE + pidz * stride_zrpe + headid * stride_hrpe + columnid * BLOCK + rowid * BLOCK * stride_srpe + rxm * stride_srpe + rxn
        rpe = tl.load(prpe, mask=check, other=0)
        x = x + rpe
    # apply key-padding mask
    if APPLY_KP_MASK:
        pkp_m = KP_M + pidz * stride_zkpm + columnid * BLOCK + rxn
        kp_m = tl.load(pkp_m, mask=check, other=-float('inf'))
        if KP_MASK_MUL:
            kp_m = tl.where(kp_m == 0, -float('inf'), 0.)
        x = x + kp_m
    # apply attention mask
    if APPLY_ATTN_MASK:
        pattn_m = ATTN_M + columnid * BLOCK + rowid * BLOCK * stride_zattnm + rxm * stride_zattnm + rxn
        attn_m = tl.load(pattn_m, mask=check, other=-float('inf'))
        if ATTN_MASK_MUL:
            attn_m = tl.where(attn_m == 0, -float('inf'), 0.)
        x = x + attn_m
    # apply causal mask
    is_in_upper_triangle = columnid * BLOCK + rxn > rowid * BLOCK + rxm
    x = x + tl.where(is_in_upper_triangle & is_causal, -float('inf'), 0.)
    # computation
    x = tl.softmax(x)
    tl.store(px, x, mask=check)


@triton.heuristics({'num_warps': lambda nargs: num_warps(nargs['sizemax'] * nargs['BLOCK'])})
@triton.heuristics({'TN': lambda nargs: triton.next_power_of_2(nargs['sizemax']) * nargs['BLOCK']})
@triton.jit
def _backward(X, scale, DX, LUT, sizemax, stride_zx, stride_zdx, TN: tl.constexpr, BLOCK: tl.constexpr):
    pidhm = tl.program_id(0)
    pidz = tl.program_id(1)
    # create index ranges
    rxm = pidhm % BLOCK
    rbm = pidhm // BLOCK
    rxn = tl.arange(0, TN) % BLOCK
    rbn = tl.arange(0, TN) // BLOCK
    # extract information from look-up table
    header = LUT + rbm * 2
    size = tl.load(header + 0)
    offset = tl.load(header + 1)
    # bounds checking on lut
    check = rbn < size
    rbmn = tl.where(check, rbn, size - 1)
    # initialize pointers to block-sparse input
    blockid = tl.load(LUT + offset + rbmn * 4)
    X = X + pidz * stride_zx + blockid * BLOCK * BLOCK + rxm * BLOCK + rxn
    DX = DX + pidz * stride_zdx + blockid * BLOCK * BLOCK + rxm * BLOCK + rxn
    # compute fused softmax backward
    x = tl.load(X, mask=check, other=0)
    dx = tl.load(DX, mask=check, other=0)
    x = x.to(tl.float32)
    dx = dx.to(tl.float32)
    y = x * (dx - tl.sum(x * dx, 0)) * scale
    tl.store(DX, y, mask=check)


class _softmax(torch.autograd.Function):
    @staticmethod
    def make_lut(layout, block, device):
        # sizes along rows
        sizes = layout.sum(-1).view(-1)
        # offsets in block format
        offsets = torch.zeros_like(sizes)
        offsets[1:] = torch.cumsum(sizes[:-1], dim=0)
        # block indices
        layout_sum = sizes.sum()
        idx = torch.arange(layout_sum, device=layout.device)
        layout_nonzero = layout.nonzero(as_tuple=False)
        head = layout_nonzero[:, 0]
        rows = layout_nonzero[:, 1]
        columns = layout_nonzero[:, 2]
        core = torch.stack((idx, columns, rows, head), dim=1).view(-1)
        # construct look-up table
        offsets = offsets * 4 + 2 * sizes.numel()
        header = torch.stack((sizes, offsets), dim=1).view(-1)
        lut = torch.cat((header, core)).type(torch.int32).to(device)
        return lut, int(sizes.max())

    @staticmethod
    def forward(
        ctx, x, scale, rpe,
        key_padding_mask, attn_mask,
        kp_mask_mode, attn_mask_mode,
        is_causal,
        spdims, block, lut, maxlut
    ):
        apply_scale = False if scale == 1.0 else True
        # handle None rpe
        if rpe is None:
            apply_rpe = False
            stride_zrpe, stride_hrpe, stride_srpe = 0, 0, 0
            rpe = torch.empty(0, dtype=x.dtype, device=x.device)
        else:
            apply_rpe = True
            stride_zrpe, stride_hrpe, stride_srpe = rpe.stride(0), rpe.stride(1), rpe.stride(2)
        # handle None key_padding_mask
        if key_padding_mask is None:
            apply_kp_mask = False
            stride_zkpm = 0
            key_padding_mask = torch.empty(0, dtype=x.dtype, device=x.device)
        else:
            apply_kp_mask = True
            stride_zkpm = key_padding_mask.stride(0)
        # handle None attention_mask
        if attn_mask is None:
            apply_attn_mask = False
            stride_zattnm = 0
            attn_mask = torch.empty(0, dtype=x.dtype, device=x.device)
        else:
            apply_attn_mask = True
            stride_zattnm = attn_mask.stride(0)
        # run kernel
        M = x.shape[0]
        grid = [spdims[0] * spdims[1] * block, M]
        _forward[grid](x, scale, lut, rpe, key_padding_mask, attn_mask, is_causal, maxlut, x.stride(0),
                       stride_zrpe, stride_hrpe, stride_srpe, stride_zkpm, stride_zattnm,
                       BLOCK=block,
                       APPLY_SCALE=apply_scale,
                       APPLY_RPE=apply_rpe,
                       APPLY_KP_MASK=apply_kp_mask,
                       APPLY_ATTN_MASK=apply_attn_mask,
                       KP_MASK_MUL=(kp_mask_mode == 'mul'),
                       ATTN_MASK_MUL=(attn_mask_mode == 'mul'))
        # save to context
        ctx.mark_dirty(x)
        ctx.save_for_backward(x, lut)
        ctx.spdims = spdims
        ctx.block = block
        ctx.maxlut = maxlut
        ctx.scale = scale
        ctx.apply_scale = apply_scale
        ctx.apply_rpe = apply_rpe
        ctx.apply_kp_mask = apply_kp_mask
        ctx.apply_attn_mask = apply_attn_mask
        ctx.kp_mask_mode = kp_mask_mode
        ctx.attn_mask_mode = attn_mask_mode
        return x

    @staticmethod
    def backward(ctx, dx):
        # retrieve from context
        x, lut = ctx.saved_tensors
        # run kernel
        M = x.shape[0]
        grid = lambda opt: [ctx.spdims[0] * ctx.spdims[1] * ctx.block, M]
        _backward[grid](x, ctx.scale, dx, lut, ctx.maxlut, x.stride(0), dx.stride(0), BLOCK=ctx.block)
        return dx, None, None, None, None, None, None, None, None, None, None, None, None, None, None


class softmax:

    def make_lut(self, device):
        key = (device, )
        if key not in self.lut_cache:
            self.lut_cache[key] = _softmax.make_lut(self.layout, self.block, device)
        return self.lut_cache[key]

    def __init__(self, layout, block):
        self.spdims = layout.shape
        self.layout = layout
        self.block = block
        self.lut_cache = dict()

    def __call__(
        self, x, scale=1., rpe=None,
        key_padding_mask=None, attn_mask=None,
        key_padding_mask_mode='add', attn_mask_mode='add',
        is_causal=False
    ):
        if rpe is not None and rpe.dtype != x.dtype:
            raise ValueError('relative position embedding must be %s' % x.dtype)
        if attn_mask is not None and attn_mask.dtype != x.dtype:
            raise ValueError('Attention mask must be %s' % x.dtype)
        if key_padding_mask is not None and key_padding_mask.dtype != x.dtype:
            raise ValueError('Key padding mask must be %s' % x.dtype)
        lut, maxlut = self.make_lut(x.device)
        x = _softmax.apply(
            x, scale, rpe,
            key_padding_mask, attn_mask,
            key_padding_mask_mode, attn_mask_mode,
            is_causal,
            self.spdims, self.block,
            lut, maxlut
        )
        return x
