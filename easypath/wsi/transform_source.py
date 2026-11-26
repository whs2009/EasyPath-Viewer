from __future__ import annotations

from io import BytesIO

import numpy as np

from PIL import Image

from PIL import ImageCms

from .ep_format import EPError

from .ep_format import MAX_REGION_PIXELS

class _Color:
    def __init__(self, profile, *, assume_srgb, image_role=None):
        if type(assume_srgb) is not bool:
            raise EPError("The source sRGB assumption must be explicit.")
        self.profile = profile
        self.transform = None
        if profile is None:
            if not assume_srgb:
                detail = f" ({image_role})" if image_role else ""
                raise EPError("Source color profile is missing" + detail + "; explicitly confirm the sRGB assumption.")
        else:
            if type(profile) is not bytes or not 128 <= len(profile) <= 1024 * 1024:
                raise EPError("Source color profile exceeds its bounds.")
            try:
                source = ImageCms.ImageCmsProfile(BytesIO(profile))
                self.transform = ImageCms.buildTransformFromOpenProfiles(
                    source, ImageCms.createProfile("sRGB"), "RGB", "RGB",
                    renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
            except (ValueError, OSError, ImageCms.PyCMSError) as error:
                raise EPError("Source ICC profile cannot be converted to RGB sRGB.") from error

    def apply(self, image):
        if image.mode not in {"RGB", "RGBA"} or image.width * image.height > MAX_REGION_PIXELS:
            raise EPError("Transform input must be bounded RGB8 or RGBA8.")
        alpha = image.getchannel("A") if image.mode == "RGBA" else None
        rgb = image.convert("RGB")
        if self.transform is not None:
            rgb = ImageCms.applyTransform(rgb, self.transform)
        if alpha is not None:
            rgb = Image.composite(rgb, Image.new("RGB", rgb.size, "white"), alpha)
        return np.array(rgb, dtype=np.uint8, copy=True)
