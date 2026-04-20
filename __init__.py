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
from . import importer
from .gltf import constants, types, buffer, serialize
from .export import converter, mesh, material, texture, scene, animation, skin
from .import_ import (
    converter as import_converter,
    buffer_reader,
    mesh as import_mesh,
    material as import_material,
    texture as import_texture,
    scene as import_scene,
    animation as import_animation,
    skin as import_skin,
)

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
    animation = importlib.reload(animation)
    skin = importlib.reload(skin)
    importer = importlib.reload(importer)
    import_converter = importlib.reload(import_converter)
    buffer_reader = importlib.reload(buffer_reader)
    import_mesh = importlib.reload(import_mesh)
    import_material = importlib.reload(import_material)
    import_texture = importlib.reload(import_texture)
    import_scene = importlib.reload(import_scene)
    import_animation = importlib.reload(import_animation)
    import_skin = importlib.reload(import_skin)


def register():
    operator.register()


def unregister():
    operator.unregister()
