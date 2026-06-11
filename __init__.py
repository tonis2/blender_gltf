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
from . import layer_node
from . import interactivity_nodes
from .gltf import constants, types, buffer, serialize
from .export import converter, mesh, material, texture, scene, animation, skin, physics, particles, interactivity, quantize, audio
from .import_ import (
    converter as import_converter,
    buffer_reader,
    mesh as import_mesh,
    material as import_material,
    texture as import_texture,
    scene as import_scene,
    animation as import_animation,
    skin as import_skin,
    physics as import_physics,
    particles as import_particles,
    interactivity as import_interactivity,
    audio as import_audio,
)

if _needs_reload:
    import importlib
    exporter = importlib.reload(exporter)
    operator = importlib.reload(operator)
    layer_node = importlib.reload(layer_node)
    interactivity_nodes = importlib.reload(interactivity_nodes)
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
    physics = importlib.reload(physics)
    particles = importlib.reload(particles)
    interactivity = importlib.reload(interactivity)
    quantize = importlib.reload(quantize)
    audio = importlib.reload(audio)
    importer = importlib.reload(importer)
    import_converter = importlib.reload(import_converter)
    buffer_reader = importlib.reload(buffer_reader)
    import_mesh = importlib.reload(import_mesh)
    import_material = importlib.reload(import_material)
    import_texture = importlib.reload(import_texture)
    import_scene = importlib.reload(import_scene)
    import_animation = importlib.reload(import_animation)
    import_skin = importlib.reload(import_skin)
    import_physics = importlib.reload(import_physics)
    import_particles = importlib.reload(import_particles)
    import_interactivity = importlib.reload(import_interactivity)
    import_audio = importlib.reload(import_audio)


def register():
    operator.register()
    layer_node.register()
    interactivity_nodes.register()


def unregister():
    interactivity_nodes.unregister()
    layer_node.unregister()
    operator.unregister()
