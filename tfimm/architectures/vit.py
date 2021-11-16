from dataclasses import dataclass
from typing import Optional, Tuple, Union

import tensorflow as tf

from tfimm.layers import act_layer_factory, norm_layer_factory
from tfimm.models import ModelConfig, keras_serializable, register_model
from tfimm.utils import (
    IMAGENET_DEFAULT_MEAN,
    IMAGENET_DEFAULT_STD,
    IMAGENET_INCEPTION_MEAN,
    IMAGENET_INCEPTION_STD,
    to_2tuple,
)

# model_registry will add each entrypoint fn to this
__all__ = ["ViT", "ViTConfig"]


@dataclass
class ViTConfig(ModelConfig):
    nb_classes: int = 1000
    in_chans: int = 3
    input_size: Union[int, Tuple[int, int]] = (224, 224)
    patch_size: Union[int, Tuple[int, int]] = (16, 16)
    embed_dim: int = 768
    depth: int = 12
    nb_heads: int = 12
    mlp_ratio: float = 4.0
    qkv_bias: bool = True
    representation_size: Optional[int] = None
    distilled: bool = False
    drop_rate: float = 0.0
    attn_drop_rate: float = 0.0
    norm_layer: str = "layer_norm"
    act_layer: str = "gelu"
    # Parameters for inference
    crop_pct: float = 0.875
    interpolation: str = "bicubic"
    mean: float = IMAGENET_INCEPTION_MEAN
    std: float = IMAGENET_INCEPTION_STD
    first_conv: str = "patch_embed/proj"
    # DeiT models have two classifier heads, one for distillation
    classifier: Union[str, Tuple[str, str]] = "head"

    """
    Args:
        num_classes: number of classes for classification head
        in_chans: number of input channels
        input_size: input image size
        patch_size: Patch size; Image size must be multiple of patch size
        embed_dim: Embedding dimension
        depth: Depth of transformer (number of encoder blocks)
        nb_heads: Number of self-attention heads
        mlp_ratio: ratio of mlp hidden dim to embedding dim
        qkv_bias: enable bias for qkv if True
        representation_size: enable and set representation layer (pre-logits) to this
            value if set
        distilled: model includes a distillation token and head as in DeiT models
        drop_rate: dropout rate
        attn_drop_rate: attention dropout rate
        norm_layer: normalization layer
        act_layer: activation function
    """

    def __post_init__(self):
        self.input_size = to_2tuple(self.input_size)
        self.patch_size = to_2tuple(self.patch_size)

    @property
    def nb_tokens(self) -> int:
        return 2 if self.distilled else 1

    @property
    def grid_size(self) -> Tuple[int, int]:
        return (
            self.input_size[0] // self.patch_size[0],
            self.input_size[1] // self.patch_size[1],
        )

    @property
    def nb_patches(self) -> int:
        return self.grid_size[0] * self.grid_size[1]


class PatchEmbeddings(tf.keras.layers.Layer):
    """
    Image to Patch Embedding.
    """

    def __init__(self, cfg: ViTConfig, **kwargs):
        super().__init__(**kwargs)
        self.patch_size = cfg.patch_size
        self.embed_dim = cfg.embed_dim

        self.projection = tf.keras.layers.Conv2D(
            filters=self.embed_dim,
            kernel_size=self.patch_size,
            strides=self.patch_size,
            use_bias=True,
            name="proj",
        )

    def call(self, x):
        emb = self.projection(x)

        # Change the 2D spatial dimensions to a single temporal dimension.
        # shape = (batch_size, num_patches, out_channels=embed_dim)
        batch_size, height, width = tf.unstack(tf.shape(x)[:3])
        num_patches = (width // self.patch_size[1]) * (height // self.patch_size[0])
        emb = tf.reshape(tensor=emb, shape=(batch_size, num_patches, -1))

        return emb


class MultiHeadAttention(tf.keras.layers.Layer):
    def __init__(self, cfg: ViTConfig, **kwargs):
        super().__init__(**kwargs)
        head_dim = cfg.embed_dim // cfg.nb_heads
        self.scale = head_dim ** -0.5
        self.cfg = cfg

        self.qkv = tf.keras.layers.Dense(
            units=3 * cfg.embed_dim, use_bias=cfg.qkv_bias, name="qkv"
        )
        self.attn_drop = tf.keras.layers.Dropout(rate=cfg.attn_drop_rate)
        self.proj = tf.keras.layers.Dense(units=cfg.embed_dim, name="proj")
        self.proj_drop = tf.keras.layers.Dropout(rate=cfg.drop_rate)

    def call(self, x, training=False):
        # B (batch size), N (sequence length), D (embedding dimension),
        # H (number of heads)
        batch_size, seq_length = tf.unstack(tf.shape(x)[:2])
        qkv = self.qkv(x)  # (B, N, 3*D)
        qkv = tf.reshape(qkv, (batch_size, seq_length, 3, self.cfg.nb_heads, -1))
        qkv = tf.transpose(qkv, (2, 0, 3, 1, 4))  # (3, B, H, N, D/H)
        q, k, v = qkv[0], qkv[1], qkv[2]

        attn = self.scale * tf.linalg.matmul(q, k, transpose_b=True)  # (B, H, N, N)
        attn = tf.nn.softmax(attn, axis=-1)  # (B, H, N, N)
        attn = self.attn_drop(attn, training)

        x = tf.linalg.matmul(attn, v)  # (B, H, N, D/H)
        x = tf.transpose(x, (0, 2, 1, 3))  # (B, N, H, D/H)
        x = tf.reshape(x, (batch_size, seq_length, -1))  # (B, N, D)

        x = self.proj(x)
        x = self.proj_drop(x, training)
        return x


class MLP(tf.keras.layers.Layer):
    """MLP as used in Vision Transformer, MLP-Mixer and related networks"""

    def __init__(self, cfg: ViTConfig, **kwargs):
        super().__init__(**kwargs)
        self.act_layer = act_layer_factory(cfg.act_layer)

        self.fc1 = tf.keras.layers.Dense(
            units=int(cfg.embed_dim * cfg.mlp_ratio), name="fc1"
        )
        self.act = self.act_layer()
        self.drop1 = tf.keras.layers.Dropout(rate=cfg.drop_rate)
        self.fc2 = tf.keras.layers.Dense(units=cfg.embed_dim, name="fc2")
        self.drop2 = tf.keras.layers.Dropout(rate=cfg.drop_rate)

    def call(self, x, training=False):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop1(x, training)
        x = self.fc2(x)
        x = self.drop2(x, training)
        return x


class Block(tf.keras.layers.Layer):
    def __init__(self, cfg: ViTConfig, **kwargs):
        super().__init__(**kwargs)
        self.norm_layer = norm_layer_factory(cfg.norm_layer)

        self.norm1 = self.norm_layer(name="norm1")
        self.attn = MultiHeadAttention(cfg, name="attn")
        self.norm2 = self.norm_layer(name="norm2")
        self.mlp = MLP(cfg, name="mlp")

    def call(self, x, training=False):
        x = x + self.attn(self.norm1(x), training)
        x = x + self.mlp(self.norm2(x), training)
        return x


@keras_serializable
class ViT(tf.keras.Model):
    cfg_class = ViTConfig

    def __init__(self, cfg: ViTConfig, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.nb_features = cfg.embed_dim  # For consistency with other models
        self.norm_layer = norm_layer_factory(cfg.norm_layer)
        self.cfg = cfg

        self.patch_embed = PatchEmbeddings(cfg, name="patch_embed")
        self.cls_token = self.add_weight(
            shape=(1, 1, cfg.embed_dim),
            initializer="zeros",
            trainable=True,
            name="cls_token",
        )
        self.dist_token = (
            self.add_weight(
                shape=(1, 1, cfg.embed_dim),
                initializer="zeros",
                trainable=True,
                name="dist_token",
            )
            if cfg.distilled
            else None
        )
        self.pos_embed = self.add_weight(
            shape=(1, cfg.nb_patches + cfg.nb_tokens, cfg.embed_dim),
            initializer="zeros",
            trainable=True,
            name="pos_embed",
        )
        self.pos_drop = tf.keras.layers.Dropout(rate=cfg.drop_rate)

        # Note: We did not implement stochastic depth, since none of the pretrained
        # timm models use it
        self.blocks = [Block(cfg, name=f"blocks/{j}") for j in range(cfg.depth)]
        self.norm = self.norm_layer(name="norm")

        # Some models have a representation layer on top of cls token
        if cfg.representation_size:
            if cfg.distilled:
                raise ValueError(
                    "Cannot combine distillation token and a representation layer."
                )
            self.nb_features = cfg.representation_size
            self.pre_logits = tf.keras.layers.Dense(
                units=cfg.representation_size, activation="tanh", name="pre_logits/fc"
            )
        else:
            self.pre_logits = None

        # Classifier head(s)
        self.head = (
            tf.keras.layers.Dense(units=cfg.nb_classes, name="head")
            if cfg.nb_classes > 0
            else tf.keras.layers.Activation("linear")  # Identity layer
        )
        self.head_dist = (
            tf.keras.layers.Dense(units=cfg.nb_classes, name="head_dist")
            if cfg.distilled and cfg.nb_classes > 0
            else tf.keras.layers.Activation("linear")  # Identity layer
        )

    @property
    def dummy_inputs(self) -> tf.Tensor:
        return tf.zeros((1, *self.cfg.input_size, self.cfg.in_chans))

    @tf.function
    def interpolate_pos_embed(self, height: int, width: int):
        """
        This method allows to interpolate the pre-trained position encodings, to be
        able to use the model on higher resolution images.

        Args:
            height: Target image height
            width: Target image width

        Returns:
            Position embeddings (including class tokens) appropriate to image of size
                (height, width)
        """
        cfg = self.cfg

        if (height == cfg.input_size[0]) and (width == cfg.input_size[1]):
            return self.pos_embed  # No interpolation needed

        src_pos_embed = self.pos_embed[:, cfg.nb_tokens :]
        src_pos_embed = tf.reshape(
            src_pos_embed, shape=(1, *cfg.grid_size, cfg.embed_dim)
        )
        tgt_grid_size = (height // cfg.patch_size[0], width // cfg.patch_size[1])
        tgt_pos_embed = tf.image.resize(
            images=src_pos_embed,
            size=tgt_grid_size,
            method="bicubic",
        )
        tgt_pos_embed = tf.reshape(tgt_pos_embed, shape=(1, -1, cfg.embed_dim))
        tgt_pos_embed = tf.concat(
            (self.pos_embed[:, : cfg.nb_tokens], tgt_pos_embed), axis=1
        )
        return tgt_pos_embed

    def forward_features(self, x, training=False):
        batch_size, height, width = tf.unstack(tf.shape(x)[:3])

        x = self.patch_embed(x)
        cls_token = tf.repeat(self.cls_token, repeats=batch_size, axis=0)
        if not self.cfg.distilled:
            x = tf.concat((cls_token, x), axis=1)
        else:
            dist_token = tf.repeat(self.dist_token, repeats=batch_size, axis=0)
            x = tf.concat((cls_token, dist_token, x), axis=1)
        x = x + self.interpolate_pos_embed(height, width)
        x = self.pos_drop(x)

        for block in self.blocks:
            x = block(x, training)
        x = self.norm(x)

        if self.cfg.distilled:
            # Here we diverge from timm and return both outputs as one tensor. That way
            # all models always have one output by default
            return x[:, :2]
        elif self.cfg.representation_size:
            return self.pre_logits(x[:, 0])
        else:
            return x[:, 0]

    def call(self, x, training=False):
        x = self.forward_features(x, training)
        if not self.cfg.distilled:
            x = self.head(x)
        else:
            y = self.head(x[:, 0])
            y_dist = self.head_dist(x[:, 1])
            x = tf.stack((y, y_dist), axis=1)
        return x


@register_model
def vit_tiny_patch16_224():
    """ViT-Tiny (Vit-Ti/16)"""
    cfg = ViTConfig(
        name="vit_tiny_patch16_224",
        url="",
        patch_size=16,
        embed_dim=192,
        depth=12,
        nb_heads=3,
    )
    return ViT, cfg


@register_model
def vit_tiny_patch16_384():
    """ViT-Tiny (Vit-Ti/16) @ 384x384."""
    cfg = ViTConfig(
        name="vit_tiny_patch16_384",
        url="",
        input_size=(384, 384),
        patch_size=16,
        embed_dim=192,
        depth=12,
        nb_heads=3,
        crop_pct=1.0,
    )
    return ViT, cfg


@register_model
def vit_small_patch32_224():
    """ViT-Small (ViT-S/32)"""
    cfg = ViTConfig(
        name="vit_small_patch32_224",
        url="",
        patch_size=32,
        embed_dim=384,
        depth=12,
        nb_heads=6,
    )
    return ViT, cfg


@register_model
def vit_small_patch32_384():
    """ViT-Small (ViT-S/32) at 384x384."""
    cfg = ViTConfig(
        name="vit_small_patch32_384",
        url="",
        input_size=(384, 384),
        patch_size=32,
        embed_dim=384,
        depth=12,
        nb_heads=6,
        crop_pct=1.0,
    )
    return ViT, cfg


@register_model
def vit_small_patch16_224():
    """ViT-Small (ViT-S/16)"""
    cfg = ViTConfig(
        name="vit_small_patch16_224",
        url="",
        patch_size=16,
        embed_dim=384,
        depth=12,
        nb_heads=6,
    )
    return ViT, cfg


@register_model
def vit_small_patch16_384():
    """ViT-Small (ViT-S/16)"""
    cfg = ViTConfig(
        name="vit_small_patch16_384",
        url="",
        input_size=(384, 384),
        patch_size=16,
        embed_dim=384,
        depth=12,
        nb_heads=6,
        crop_pct=1.0,
    )
    return ViT, cfg


@register_model
def vit_base_patch32_224():
    """
    ViT-Base (ViT-B/32) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k, source
    https://github.com/google-research/vision_transformer.
    """
    cfg = ViTConfig(
        name="vit_base_patch32_224",
        url="",
        patch_size=32,
        embed_dim=768,
        depth=12,
        nb_heads=12,
    )
    return ViT, cfg


@register_model
def vit_base_patch32_384():
    """
    ViT-Base model (ViT-B/32) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 384x384, source
    https://github.com/google-research/vision_transformer.
    """
    cfg = ViTConfig(
        name="vit_base_patch32_384",
        url="",
        input_size=(384, 384),
        patch_size=32,
        embed_dim=768,
        depth=12,
        nb_heads=12,
        crop_pct=1.0,
    )
    return ViT, cfg


@register_model
def vit_base_patch16_224():
    """
    ViT-Base (ViT-B/16) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 224x224, source
    https://github.com/google-research/vision_transformer.
    """
    cfg = ViTConfig(
        name="vit_base_patch16_224",
        url="",
        patch_size=16,
        embed_dim=768,
        depth=12,
        nb_heads=12,
    )
    return ViT, cfg


@register_model
def vit_base_patch16_384():
    """
    ViT-Base model (ViT-B/16) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 384x384, source
    https://github.com/google-research/vision_transformer.
    """
    cfg = ViTConfig(
        name="vit_base_patch16_384",
        url="",
        input_size=(384, 384),
        patch_size=16,
        embed_dim=768,
        depth=12,
        nb_heads=12,
        crop_pct=1.0,
    )
    return ViT, cfg


@register_model
def vit_large_patch32_224():
    """
    ViT-Large model (ViT-L/32) from original paper (https://arxiv.org/abs/2010.11929).
    No pretrained weights.
    """
    cfg = ViTConfig(
        name="vit_large_patch32_224",
        url="",
        patch_size=32,
        embed_dim=1024,
        depth=24,
        nb_heads=16,
    )
    return ViT, cfg


@register_model
def vit_large_patch32_384():
    """
    ViT-Large model (ViT-L/32) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 384x384, source
    https://github.com/google-research/vision_transformer.
    """
    cfg = ViTConfig(
        name="vit_large_patch32_384",
        url="",
        input_size=(384, 384),
        patch_size=32,
        embed_dim=1024,
        depth=24,
        nb_heads=16,
        crop_pct=1.0,
    )
    return ViT, cfg


@register_model
def vit_large_patch16_224():
    """
    ViT-Large model (ViT-L/32) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 224x224, source
    https://github.com/google-research/vision_transformer.
    """
    cfg = ViTConfig(
        name="vit_large_patch16_224",
        url="",
        patch_size=16,
        embed_dim=1024,
        depth=24,
        nb_heads=16,
    )
    return ViT, cfg


@register_model
def vit_large_patch16_384():
    """
    ViT-Large model (ViT-L/16) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-1k weights fine-tuned from in21k @ 384x384, source
    https://github.com/google-research/vision_transformer.
    """
    cfg = ViTConfig(
        name="vit_large_patch16_384",
        url="",
        input_size=(384, 384),
        patch_size=16,
        embed_dim=1024,
        depth=24,
        nb_heads=16,
        crop_pct=1.0,
    )
    return ViT, cfg


@register_model
def vit_base_patch32_sam_224():
    """
    ViT-Base (ViT-B/32) w/ SAM pretrained weights.
    Paper: https://arxiv.org/abs/2106.01548
    """
    cfg = ViTConfig(
        name="vit_base_patch32_sam_224",
        url="",
        patch_size=32,
        embed_dim=768,
        depth=12,
        nb_heads=12,
    )
    return ViT, cfg


@register_model
def vit_base_patch16_sam_224():
    """
    ViT-Base (ViT-B/16) w/ SAM pretrained weights.
    Paper: https://arxiv.org/abs/2106.01548
    """
    cfg = ViTConfig(
        name="vit_base_patch16_sam_224",
        url="",
        patch_size=16,
        embed_dim=768,
        depth=12,
        nb_heads=12,
    )
    return ViT, cfg


@register_model
def vit_tiny_patch16_224_in21k():
    """
    ViT-Tiny (Vit-Ti/16). ImageNet-21k weights @ 224x224, source
    https://github.com/google-research/vision_transformer.
    Note: This model has a valid 21k classifier head and no representation layer.
    """
    cfg = ViTConfig(
        name="vit_tiny_patch16_224_in21k",
        url="",
        nb_classes=21843,
        patch_size=16,
        embed_dim=192,
        depth=12,
        nb_heads=3,
    )
    return ViT, cfg


@register_model
def vit_small_patch32_224_in21k():
    """
    ViT-Small (ViT-S/16) ImageNet-21k weights @ 224x224, source
    https://github.com/google-research/vision_transformer.
    Note: This model has a valid 21k classifier head and no representation layer.
    """
    cfg = ViTConfig(
        name="vit_small_patch32_224_in21k",
        url="",
        nb_classes=21843,
        patch_size=32,
        embed_dim=384,
        depth=12,
        nb_heads=6,
    )
    return ViT, cfg


@register_model
def vit_small_patch16_224_in21k():
    """
    ViT-Small (ViT-S/16) ImageNet-21k weights @ 224x224, source
    https://github.com/google-research/vision_transformer.
    Note: This model has a valid 21k classifier head and no representation layer.
    """
    cfg = ViTConfig(
        name="vit_small_patch16_224_in21k",
        url="",
        nb_classes=21843,
        patch_size=16,
        embed_dim=384,
        depth=12,
        nb_heads=6,
    )
    return ViT, cfg


@register_model
def vit_base_patch32_224_in21k():
    """
    ViT-Base model (ViT-B/32) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-21k weights @ 224x224, source
    https://github.com/google-research/vision_transformer.
    Note: This model has a valid 21k classifier head and no representation layer.
    """
    cfg = ViTConfig(
        name="vit_base_patch32_224_in21k",
        url="",
        nb_classes=21843,
        patch_size=32,
        embed_dim=768,
        depth=12,
        nb_heads=12,
    )
    return ViT, cfg


@register_model
def vit_base_patch16_224_in21k():
    """
    ViT-Base model (ViT-B/16) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-21k weights @ 224x224, source
    https://github.com/google-research/vision_transformer.
    Note: This model has a valid 21k classifier head and no representation layer.
    """
    cfg = ViTConfig(
        name="vit_base_patch16_224_in21k",
        url="",
        nb_classes=21843,
        patch_size=16,
        embed_dim=768,
        depth=12,
        nb_heads=12,
    )
    return ViT, cfg


@register_model
def vit_large_patch32_224_in21k():
    """
    ViT-Large model (ViT-L/32) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-21k weights @ 224x224, source
    https://github.com/google-research/vision_transformer.
    Note: This model has a representation layer but the 21k classifier head is zero'd
    out in original weights.
    """
    cfg = ViTConfig(
        name="vit_large_patch32_224_in21k",
        url="",
        nb_classes=21843,
        patch_size=32,
        embed_dim=1024,
        depth=24,
        nb_heads=16,
        representation_size=1024,
    )
    return ViT, cfg


@register_model
def vit_large_patch16_224_in21k():
    """
    ViT-Large model (ViT-L/16) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-21k weights @ 224x224, source
    https://github.com/google-research/vision_transformer.
    Note: This model has a valid 21k classifier head and no representation layer.
    """
    cfg = ViTConfig(
        name="vit_large_patch16_224_in21k",
        url="",
        nb_classes=21843,
        patch_size=16,
        embed_dim=1024,
        depth=24,
        nb_heads=16,
    )
    return ViT, cfg


@register_model
def vit_huge_patch14_224_in21k():
    """
    ViT-Huge model (ViT-H/14) from original paper (https://arxiv.org/abs/2010.11929).
    ImageNet-21k weights @ 224x224, source
    https://github.com/google-research/vision_transformer.
    Note: This model has a representation layer but the 21k classifier head is zero'd
    out in original weights.
    """
    cfg = ViTConfig(
        name="vit_huge_patch14_224_in21k",
        url="",
        nb_classes=21843,
        patch_size=14,
        embed_dim=1280,
        depth=32,
        nb_heads=16,
        representation_size=1280,
    )
    return ViT, cfg


@register_model
def deit_tiny_patch16_224():
    """
    DeiT-tiny model @ 224x224 from paper (https://arxiv.org/abs/2012.12877).
    ImageNet-1k weights from https://github.com/facebookresearch/deit.
    """
    cfg = ViTConfig(
        name="deit_tiny_patch16_224",
        url="",
        patch_size=16,
        embed_dim=192,
        depth=12,
        nb_heads=3,
        mean=IMAGENET_DEFAULT_MEAN,
        std=IMAGENET_DEFAULT_STD,
    )
    return ViT, cfg


@register_model
def deit_small_patch16_224():
    """
    DeiT-small model @ 224x224 from paper (https://arxiv.org/abs/2012.12877).
    ImageNet-1k weights from https://github.com/facebookresearch/deit.
    """
    cfg = ViTConfig(
        name="deit_small_patch16_224",
        url="",
        patch_size=16,
        embed_dim=384,
        depth=12,
        nb_heads=6,
        mean=IMAGENET_DEFAULT_MEAN,
        std=IMAGENET_DEFAULT_STD,
    )
    return ViT, cfg


@register_model
def deit_base_patch16_224():
    """
    DeiT base model @ 224x224 from paper (https://arxiv.org/abs/2012.12877).
    ImageNet-1k weights from https://github.com/facebookresearch/deit.
    """
    cfg = ViTConfig(
        name="deit_base_patch16_224",
        url="",
        patch_size=16,
        embed_dim=768,
        depth=12,
        nb_heads=12,
        mean=IMAGENET_DEFAULT_MEAN,
        std=IMAGENET_DEFAULT_STD,
    )
    return ViT, cfg


@register_model
def deit_base_patch16_384():
    """
    DeiT base model @ 384x384 from paper (https://arxiv.org/abs/2012.12877).
    ImageNet-1k weights from https://github.com/facebookresearch/deit.
    """
    cfg = ViTConfig(
        name="deit_base_patch16_384",
        url="",
        input_size=(384, 384),
        patch_size=16,
        embed_dim=768,
        depth=12,
        nb_heads=12,
        crop_pct=1.0,
        mean=IMAGENET_DEFAULT_MEAN,
        std=IMAGENET_DEFAULT_STD,
    )
    return ViT, cfg


@register_model
def deit_tiny_distilled_patch16_224():
    """
    DeiT-tiny distilled model @ 224x224 from paper (https://arxiv.org/abs/2012.12877).
    ImageNet-1k weights from https://github.com/facebookresearch/deit.
    """
    cfg = ViTConfig(
        name="deit_tiny_distilled_patch16_224",
        url="",
        patch_size=16,
        embed_dim=192,
        depth=12,
        nb_heads=3,
        distilled=True,
        mean=IMAGENET_DEFAULT_MEAN,
        std=IMAGENET_DEFAULT_STD,
        classifier=("head", "head_dist"),
    )
    return ViT, cfg


@register_model
def deit_small_distilled_patch16_224():
    """
    DeiT-small distilled model @ 224x224 from paper (https://arxiv.org/abs/2012.12877).
    ImageNet-1k weights from https://github.com/facebookresearch/deit.
    """
    cfg = ViTConfig(
        name="deit_small_distilled_patch16_224",
        url="",
        patch_size=16,
        embed_dim=384,
        depth=12,
        nb_heads=6,
        distilled=True,
        mean=IMAGENET_DEFAULT_MEAN,
        std=IMAGENET_DEFAULT_STD,
        classifier=("head", "head_dist"),
    )
    return ViT, cfg


@register_model
def deit_base_distilled_patch16_224():
    """
    DeiT-base distilled model @ 224x224 from paper (https://arxiv.org/abs/2012.12877).
    ImageNet-1k weights from https://github.com/facebookresearch/deit.
    """
    cfg = ViTConfig(
        name="deit_base_distilled_patch16_224",
        url="",
        patch_size=16,
        embed_dim=768,
        depth=12,
        nb_heads=12,
        distilled=True,
        mean=IMAGENET_DEFAULT_MEAN,
        std=IMAGENET_DEFAULT_STD,
        classifier=("head", "head_dist"),
    )
    return ViT, cfg


@register_model
def deit_base_distilled_patch16_384():
    """
    DeiT-base distilled model @ 384x384 from paper (https://arxiv.org/abs/2012.12877).
    ImageNet-1k weights from https://github.com/facebookresearch/deit.
    """
    cfg = ViTConfig(
        name="deit_base_distilled_patch16_384",
        url="",
        input_size=(384, 384),
        patch_size=16,
        embed_dim=768,
        depth=12,
        nb_heads=12,
        distilled=True,
        crop_pct=1.0,
        mean=IMAGENET_DEFAULT_MEAN,
        std=IMAGENET_DEFAULT_STD,
        classifier=("head", "head_dist"),
    )
    return ViT, cfg