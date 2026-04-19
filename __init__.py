bl_info = {
    "name": "glTF 2.0 Exporter (Custom)",
    "description": "Export Blender scenes to glTF 2.0 with experimental features",
    "author": "Tonis",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "File > Export > glTF 2.0 (.glb/.gltf) Custom",
    "category": "Import-Export",
}

_needs_reload = "operator" in locals()

from . import operator
from . import exporter
from .gltf import constants, types, buffer, serialize
from .export import converter, mesh, material, texture, scene

if _needs_reload:
    import importlib
    operator = importlib.reload(operator)
    exporter = importlib.reload(exporter)
    constants = importlib.reload(constants)
    types = importlib.reload(types)
    buffer = importlib.reload(buffer)
    serialize = importlib.reload(serialize)
    converter = importlib.reload(converter)
    mesh = importlib.reload(mesh)
    material = importlib.reload(material)
    texture = importlib.reload(texture)
    scene = importlib.reload(scene)


def register():
    operator.register()


def unregister():
    operator.unregister()
