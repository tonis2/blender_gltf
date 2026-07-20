"""Download the libktx native library from ktx.c3's GitHub releases.

The addon does not bundle the per-platform ktx shared libraries. When KTX2
encode/decode is wanted but the lib is missing, GLTF_OT_download_ktx asks the
user for confirmation, fetches the single release asset matching this OS/CPU
(see ktx_lib.download_url()) and installs it at ktx_lib.download_dest().
"""
from __future__ import annotations

import os
import tempfile
import urllib.error
import urllib.request

import bpy

from . import ktx_lib


class GLTF_OT_download_ktx(bpy.types.Operator):
    """Download the KTX2 encode/decode library for this platform (a few MB) from the ktx.c3 GitHub releases"""
    bl_idname = "gltf_custom.download_ktx"
    bl_label = "Download KTX Binaries"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        if ktx_lib.download_url() is None:
            self.report(
                {"ERROR"},
                "No prebuilt ktx library exists for this platform "
                "(see github.com/" + ktx_lib.GITHUB_REPO + ")",
            )
            return {"CANCELLED"}
        return context.window_manager.invoke_confirm(
            self, event,
            title="Download KTX binaries?",
            message=(f"Fetch {ktx_lib.asset_name()} from "
                     f"github.com/{ktx_lib.GITHUB_REPO} releases?"),
            confirm_text="Download",
            icon="INFO",
        )

    def execute(self, context):
        url = ktx_lib.download_url()
        dest = ktx_lib.download_dest(create=True)
        if url is None or dest is None:
            self.report({"ERROR"}, "No prebuilt ktx library for this platform")
            return {"CANCELLED"}

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "blender-gltf-addon"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            hint = " (no release asset for this platform yet?)" if e.code == 404 else ""
            self.report({"ERROR"}, f"Download failed: {e}{hint}\n{url}")
            return {"CANCELLED"}
        except (urllib.error.URLError, OSError) as e:
            self.report({"ERROR"}, f"Download failed: {e}")
            return {"CANCELLED"}

        # Write next to the destination and move into place so a failed or
        # interrupted download never leaves a half-written library behind.
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

        ktx_lib.reset()
        if not ktx_lib.is_available():
            self.report({"ERROR"},
                        f"Downloaded {dest.name} but could not load it: "
                        f"{ktx_lib.load_error()}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"KTX binaries installed: {dest}")
        return {"FINISHED"}


def register():
    bpy.utils.register_class(GLTF_OT_download_ktx)


def unregister():
    bpy.utils.unregister_class(GLTF_OT_download_ktx)
