# SPDX-License-Identifier: GPL-2.0-or-later
"""
Cycles Render Instances — front-end toggle.

Adds a checkbox in Object Properties that marks a geometry-nodes object as a
Cycles "render instancer". The patched Cycles build reads this marker and syncs
the object's geometry-nodes instances directly, bypassing object_duplilist().

This addon only authors data. It does nothing on its own — it requires the
matching Cycles patch. In stock Blender the marker is inert (a harmless custom
property that nothing reads).

Storage detail (verified, not assumed): a BoolProperty registered on the Object
type does NOT land in the object's user custom-property group (id->properties) —
on this Blender it goes to a separate group that the C++ marker check does not
read. So the checkbox is a proxy: its get/set read and write the actual custom
property obj["cycles_render_instancer"] via dict access, which is stored in
id->properties as an IDP_BOOLEAN — exactly what
IDP_GetPropertyFromGroup(id->properties, "cycles_render_instancer") reads.

Install: Edit > Preferences > Add-ons > Install, pick this file, enable it.
"""

import bpy

bl_info = {
    "name": "Cycles Render Instances",
    "author": "Samuel Mason",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "Properties > Object > Cycles Render Instances",
    "description": "Toggle direct Cycles sync of geometry-nodes instances "
                   "(requires the matching Cycles patch)",
    "category": "Render",
    "doc_url": "https://github.com/samjmason/cycles-render-instances",
}

# Must match the marker string the C++ reads verbatim.
MARKER = "cycles_render_instancer"


def _marker_get(self):
    # Dict access reads id->properties, the group the C++ marker check reads.
    return bool(self.get(MARKER, False))


def _marker_set(self, value):
    if value:
        self[MARKER] = True
    elif MARKER in self:
        # Remove rather than store False, so a disabled object carries no marker
        # at all -- cleanest for files opened in stock Blender.
        del self[MARKER]


def _object_makes_instances(obj):
    """True when geometry nodes actually produced instances on this object.

    Uses the evaluated object, so it reflects the current modifier result. Kept
    defensive: any failure just means we don't claim it makes instances.
    """
    if obj is None:
        return False
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        # A GN object that outputs instances reports them here.
        return bool(getattr(eval_obj, "is_instancer", False)) or \
            any(m.type == "NODES" for m in obj.modifiers)
    except Exception:
        return any(m.type == "NODES" for m in getattr(obj, "modifiers", []))


class CYCLES_OBJECT_PT_render_instances(bpy.types.Panel):
    bl_label = "Cycles Render Instances"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        # Only under Cycles, and only for objects that can carry the marker.
        return (
            context.engine == "CYCLES"
            and context.object is not None
            and context.object.type in {"MESH", "CURVES", "POINTCLOUD", "CURVE"}
        )

    def draw_header(self, context):
        self.layout.prop(context.object, "cycles_render_instances_ui", text="")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        obj = context.object

        enabled = getattr(obj, "cycles_render_instances_ui", False)
        col = layout.column()
        col.active = enabled

        if not _object_makes_instances(obj):
            col.label(text="No geometry-nodes instances detected.", icon="INFO")
        else:
            col.label(text="Instances synced directly (dupli bypass).",
                      icon="CHECKMARK")

        col.label(text="Requires the patched Cycles build.", icon="MODIFIER")


def register():
    # The UI checkbox. Distinct RNA name from the marker so there is no
    # ambiguity between the RNA property and the custom property it proxies;
    # get/set do the actual read/write of obj["cycles_render_instancer"].
    bpy.types.Object.cycles_render_instances_ui = bpy.props.BoolProperty(
        name="Render Instances",
        description=(
            "Sync this object's geometry-nodes instances directly in Cycles, "
            "bypassing dupli expansion. Requires the matching Cycles patch"
        ),
        default=False,
        get=_marker_get,
        set=_marker_set,
    )
    bpy.utils.register_class(CYCLES_OBJECT_PT_render_instances)


def unregister():
    bpy.utils.unregister_class(CYCLES_OBJECT_PT_render_instances)
    # Leave existing markers on objects intact; just remove the UI property
    # definition so the type is clean.
    del bpy.types.Object.cycles_render_instances_ui


if __name__ == "__main__":
    register()
