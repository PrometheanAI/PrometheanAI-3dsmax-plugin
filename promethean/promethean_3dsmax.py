import os
import re
import json
import math
import inspect
import itertools
import contextlib
from collections import OrderedDict

import pymxs
from pymxs import runtime as rt

try:
    from PySide2.QtCore import QTimer
    from PySide2.QtWidgets import QApplication
    from PySide2.QtGui import QCursor
except ImportError:
    from PySide.QtCore import QTimer
    from PySide.QtGui import QApplication, QCursor

# =====================================================================
# GOOD PRACTICES FOR 3DS MAX PLUGIN
# =====================================================================
# Naming convention:
# node/nodes -> 3dsMax nodes
# p_name/p_names -> names that we ping-pong with promethean
#
# Format for the p_name is node_name_ID
# Ideally all the conversion from names to nodes and back should be done in the switch_command function
# All the other functions should operate with nodes (almost never names)


# We store these two values for the case when we send a few commands in a row and one of them creates a new node
# while the next need to operate on the new node without getting reply from the plugin
# Example:
# set_mesh new_plate.max plate_132  <- will create a new node 'new_plate_168'
# translate ... plate_132 <- won't know about the new name
#
# So when the second command evaluated, we'll use this dictionary to retrieve the new node from the old name
# TODO: wipe the dict on scene load
old_nodes_names_dict = {}  # TODO: we should not need to have this this globally. need to remove and fix the issues that this was meant to cover up at some local level
units_multiplier = 1.0

_simulated_nodes = list()
_snap_timer = QTimer()
_snap_timer.setInterval(150)


# =====================================================================
# +++ 3DS MAX TCP MESSAGE RECEIVER
# =====================================================================
def command_switch(command_str):
    global units_multiplier
    global old_nodes_names_dict

    # for Windows
    command_str = command_str.replace("\r", "").strip()
    if not command_str:  # if str is blank
        return

    # Command name should be the first substring in the message, separated by space ' ' character.
    # The rest is considered the parameters.
    # Every command knows what parameters to expect, so it's responsible for parsing them further if needed
    command, _, command_parameters_str = command_str.partition(' ')

    msg = 'DefaultValue'  # return message

    # Get scene name or None
    if command == 'get_scene_name':
        msg = '%s%s' % (rt.maxFilePath, rt.maxFileName) or 'None'

    elif command == 'save_current_scene':
        # rt.saveMaxFile(rt.maxFileName)  # can return bool of save status if we need it
        execute('saveMaxFile "%s%s" quiet:true' % (rt.maxFilePath, rt.maxFileName))

    elif command == 'open_scene' and command_parameters_str:
        path = command_parameters_str
        if rt.CheckForSave():
            rt.loadMaxFile(path, useFileUnits=True, quiet=True)  # can return bool of save status if we need it

    # Get selection objects in 3dsmax
    elif command == 'get_selection':
        msg = str(nodes_to_promethean_names(rt.getCurrentSelection()))

    # Get visible objects in 3dsmax viewport
    elif command == 'get_visible_static_mesh_actors':
        msg = ','.join(nodes_to_promethean_names(get_geometry_in_view()))
    # Get selected and visible objects in 3dsmax viewport
    elif command == 'get_selected_and_visible_static_mesh_actors':
        # Target of a camera is geometry too
        selection_list = [x for x in rt.getCurrentSelection() if rt.superClassOf(x) == rt.GeometryClass or rt.classOf(x) == rt.Dummy]
        on_screen_list = get_geometry_in_view()
        scene_name = str(rt.maxFileName)

        selected_paths_dict = {}
        for i, obj_name in enumerate(selection_list):
            selected_paths_dict.setdefault(get_reference_path(obj_name), []).append(i)
        rendered_paths_dict = {}
        for i, obj_name in enumerate(on_screen_list):
            rendered_paths_dict.setdefault(get_reference_path(obj_name), []).append(i)
        msg = json.dumps({'selected_names': nodes_to_promethean_names(selection_list),
                          'rendered_names': nodes_to_promethean_names(on_screen_list),
                          'selected_paths': selected_paths_dict,
                          'rendered_paths': rendered_paths_dict, 'scene_name': scene_name})

    elif command == 'get_location_data' and command_parameters_str:
        obj_names = command_parameters_str.split(',')
        data_dict = {node_to_promethean_name(x): list(x.pos * units_multiplier) for x in get_nodes_by_promethean_names(obj_names) if x}
        msg = json.dumps(data_dict)

    elif command == 'get_pivot_data' and command_parameters_str:
        obj_names = command_parameters_str.split(',')
        data_dict = {node_to_promethean_name(x): list(getTransform(x)[1]) for x in
                     get_nodes_by_promethean_names(obj_names) if x}
        msg = json.dumps(data_dict)

    elif command == 'get_transform_data' and command_parameters_str:
        obj_names = command_parameters_str.split(',')
        data_dict = {}
        for obj_name in obj_names:
            node = get_node_by_promethean_name(obj_name)
            if node:
                data = getRawObjectData(node)
                data_dict[obj_name] = data['transform'] + data['size'] + data['pivot_offset'] + [data['parent_name']] # transform = t,t,t,r,r,r,s,s,s
        msg = json.dumps(data_dict)

    elif command == 'add_objects' and command_parameters_str:
        """ add a group of objects and send back a dictionary of how their proper dcc names
        takes a json string as input that is a dictionary with old dcc name ask key and this dict as value 
        (asset_path, name, location, rotation, scale, parent_dcc_name) """
        obj_dict = json.loads(command_parameters_str)
        return_dict = {}

        for old_p_name in obj_dict.keys():
            new_node = add_object(obj_dict[old_p_name])
            if new_node:
                old_nodes_names_dict[old_p_name] = new_node
                return_dict[old_p_name] = node_to_promethean_name(new_node)
        msg = json.dumps(return_dict)

    elif command == 'add_objects_from_polygons' and command_parameters_str:
        obj_list = json.loads(command_parameters_str)  # str has spaces so doing this
        add_objects_from_polygons(obj_list)

    elif command == 'add_objects_from_triangles' and command_parameters_str:
        obj_list = json.loads(command_parameters_str)  # str has spaces so doing this
        return_dict = add_objects_from_triangles(obj_list)
        msg = json.dumps(return_dict)  # return names of newly created objects

    elif command == 'parent' and command_parameters_str:
        p_names = command_parameters_str.split(',')
        nodes = get_nodes_by_promethean_names(p_names)
        parent(nodes[0], nodes[1:])

    elif command == 'unparent':
        p_names = command_parameters_str.split(',')
        nodes = get_nodes_by_promethean_names(p_names)
        unparent(nodes)

    elif command == 'match_objects' and command_parameters_str:
        # TODO change to work with IDs
        p_names = command_parameters_str.split(',')
        return_dict = {}
        transforms = [x for x in rt.objects if x.transform is not None]
        for p_name in p_names:
            for transform in transforms:
                if transform.name == p_name:
                    return_dict[p_name] = transform.name
                    break
        msg = json.dumps(return_dict)

    elif command == 'isolate_selection':
        execute('actionMan.executeAction 0 "197"')

    elif command == 'learn_file' and command_parameters_str:  # open and learn a file
        # TODO: make support for the Unit conversion
        learn_dict = json.loads(command_str.replace('learn_file ', ''))
        learn_file(learn_dict['file_path'], learn_dict['learn_file_path'], learn_dict['tags'],
                   learn_dict['project'])
        msg = json.dumps(True)  # return a message once it's done

    elif command == 'get_vertex_data_from_scene_objects' and command_parameters_str:
        dcc_names = json.loads(command_parameters_str)
        out_dict = dict()
        for dcc_name in dcc_names:
            node = get_node_by_promethean_name(dcc_name)
            if rt.superClassOf(node) == rt.GeometryClass:
                vert_list = get_triangle_positions(node)  # direction mask to adjust for thick wall objects
                vert_list = [{i: vert for i, vert in enumerate(vert_list)}]  # TODO this is to keep parity with unreal integration. should be fixed eventually
                out_dict[dcc_name] = vert_list
        msg = json.dumps(out_dict)

    elif command == 'get_vertex_data_from_scene_object' and command_parameters_str:
        # TODO: doesn't work, need to fix
        p_name = command_parameters_str
        node = get_node_by_promethean_name(p_name)
        vert_list = get_triangle_positions(node)  # direction mask to adjust for thick wall objects
        vert_list = [{i: vert for i, vert in enumerate(vert_list)}]  # TODO this is to keep parity with unreal integration. should be fixed eventually
        vert_dict = {'vertex_positions': vert_list}
        msg = json.dumps(vert_list)
        dump(vert_dict)

    elif command == 'report_done':
        msg = json.dumps('Done')  # return a message once it's done

    elif command == 'screenshot' and command_parameters_str:
        dump('start')
        path = command_parameters_str  # in case there are spaces in the path
        path.strip()
        execute("max zoomext sel all")  # set all objects in scene
        execute("viewport.setType #view_persp_user")  # set perspective view
        execute("img = gw.getViewportDib()")
        execute("img.fileName = \"" + path + "\"")
        msg = str(execute("save img"))

    elif command == 'kill':
        selection = rt.getCurrentSelection()
        for obj_name in selection:
            obj_name.name = obj_name.name.replace('_kill_', '') if '_kill_' in obj_name.name else '{}_kill_'.format(
                obj_name.name)

    elif command == 'rename' and command_parameters_str:
        source_name, target_name = command_parameters_str.split(',')
        source_obj = get_node_by_promethean_name(source_name)
        if source_obj:
            source_obj.name = target_name
        msg = source_obj.name

    elif command == 'learn' and command_parameters_str:
        # TODO: make support for the Unit conversion
        # split by the last space
        cache_file_path, from_selection = command_parameters_str.rpartition(' ')[::2]
        from_selection = from_selection == 'True'  # bool from text
        msg = str(learn(cache_file_path, [], None, from_selection))

    elif command == 'set_vertex_color' and command_parameters_str:
        color = [int(float(str(clr)) * 255.0) for clr in command_parameters_str.split(',')]
        msg = str(set_vertex_color( color))

    elif command == 'set_roughness' and command_parameters_str:
        value = command_parameters_str
        msg = str(set_roughness(value))

    elif command == 'set_metallic' and command_parameters_str:
        is_metallic, has_texture = command_parameters_str.split(' ')
        msg = str(set_metallic(is_metallic, has_texture))

    elif command == 'set_texture_tiling' and command_parameters_str:
        has_texture, is_metallic = command_parameters_str.split(' ')
        msg = str(set_texture_tiling(has_texture, is_metallic))

    elif command == 'set_uv_quadrant' and command_parameters_str:
        u, v = [int(x) for x in command_parameters_str.split(' ')]
        msg = str(set_uv_quadrant( u, v))

    elif command == 'get_vertex_colors':
        nodes = None
        if command_parameters_str:
            p_names = command_parameters_str.split(',')
            nodes = get_nodes_by_promethean_names(p_names)
        msg = json.dumps(get_vertex_colors(nodes))

    elif command == 'select_vertex_color' and command_parameters_str:
        node_ids, color = command_parameters_str.split(' ')
        if type(node_ids) != list:
            node_ids = [node_ids]
        color = [int(clr) for clr in color.split(',')]
        nodes = [rt.maxOps.getNodeByHandle(int(node_id)) for node_id in node_ids]
        msg = str(select_vertex_color(nodes, color))

    elif command == 'add_mesh_on_selection' and command_parameters_str:
        mesh_paths = command_parameters_str.split(',')
        add_meshes_on_selection(mesh_paths)

    elif command in ['translate', 'scale', 'rotate', 'translate_relative', 'scale_relative',
                     'rotate_relative'] and command_parameters_str:
        value, p_names = json.loads(command_parameters_str)
        value = [x / units_multiplier if command in ['translate', 'translate_relative'] else x
                 for x in value]
        nodes = get_nodes_by_promethean_names(p_names)
        if command == 'translate':
            func = translate
        elif command == 'scale':
            func = scale
        elif command == 'rotate':
            func = rotate
        elif command == 'translate_relative':
            func = translate_relative
        elif command == 'scale_relative':
            func = scale_relative
        elif command == 'rotate_relative':
            func = rotate_relative
        for node in nodes:
            if node:
                func(value, node)

    elif command == 'translate_and_snap' and command_parameters_str:
        location, raytrace_distance, max_normal_deviation, nodes, ignore_nodes  = json.loads(command_parameters_str)
        location = [float(x) / units_multiplier for x in location]
        raytrace_distance = float(raytrace_distance) / units_multiplier
        max_normal_deviation = float(max_normal_deviation)
        nodes = get_nodes_by_promethean_names(nodes)
        ignore_nodes = get_nodes_by_promethean_names(ignore_nodes)
        translate_and_raytrace_by_name(nodes, location, raytrace_distance, max_normal_deviation, ignore_nodes)

    elif command == 'translate_and_raytrace' and command_parameters_str:
        location, raytrace_distance, nodes, ignore_nodes  = json.loads(command_parameters_str)
        location = [float(x) / units_multiplier for x in location]
        raytrace_distance = float(raytrace_distance) / units_multiplier
        max_normal_deviation = 0
        nodes = get_nodes_by_promethean_names(nodes)
        ignore_nodes = get_nodes_by_promethean_names(ignore_nodes)
        translate_and_raytrace_by_name(nodes, location, raytrace_distance, max_normal_deviation, ignore_nodes)

    elif command == 'set_mesh' and command_parameters_str:
        mesh_path, p_names = json.loads(command_parameters_str)
        nodes = get_nodes_by_promethean_names(p_names, only_valid=True)
        set_mesh(mesh_path, nodes)

    elif command == 'set_mesh_on_selection' and command_parameters_str:
        mesh_path = command_parameters_str
        selected_nodes = rt.GetCurrentSelection()
        set_mesh(mesh_path, selected_nodes)

    elif command == 'remove' and command_parameters_str:
        p_names = command_parameters_str.split(',')
        nodes = get_nodes_by_promethean_names(p_names, only_valid=True)
        # Remove nodes from the old names dict
        old_nodes_names_dict = {key: value for key, value in old_nodes_names_dict.items() if value not in nodes}
        rt.delete(nodes)

    elif command == 'remove_descendents' and command_parameters_str:
        p_names = command_parameters_str.split(',')
        nodes = get_nodes_by_promethean_names(p_names)
        for node in nodes:
            if node:
                remove_descendants_recursively(node)

    elif command == 'set_hidden' and command_parameters_str:
        p_names = command_parameters_str.split(',')
        nodes = get_nodes_by_promethean_names(p_names)
        rt.hide(nodes)

    elif command == 'set_visible' and command_parameters_str:
        p_names = command_parameters_str.split(',')
        nodes = get_nodes_by_promethean_names(p_names)
        rt.unhide(list(filter(None, nodes)))

    elif command == 'select' and command_parameters_str:
        p_names = command_parameters_str.split(',')
        nodes = get_nodes_by_promethean_names(p_names, only_valid=True)
        rt.select(nodes)

    elif command == 'create_assets_from_selection' and command_parameters_str:
        content_folder = command_parameters_str
        exported_paths = create_asset_from_selection(root_path=content_folder)
        msg = exported_paths

    elif command == 'drop_asset':
        file_path = command_parameters_str
        load_asset(file_path)

    elif command in ['raytrace', 'raytrace_bidirectional'] and command_parameters_str:
        # raytrace_bidirectional 0,0,-1 1500 box001#2
        direction_vec, distance, p_names  = json.loads(command_parameters_str)
        direction_vec = rt.Point3(*direction_vec)
        distance = distance / units_multiplier
        out_dict = {}
        for p_name in p_names:
            node = get_node_by_promethean_name(p_name)
            # TODO: doesn't work
            if node:
                hit_object, hit_position, hit_normal = find_close_intersection_point(node, direction_vec, distance)
                out_dict[p_name] = [x * units_multiplier for x in hit_position] if hit_position else [0.0, 0.0, 0.0]
        msg = out_dict

    elif command in ('get_simulation_on_actors_by_name', 'get_transform_data_from_simulating_objects'):
        msg = 'None'

    elif command == 'enable_simulation_on_objects' and command_parameters_str:

        global _simulated_nodes
        rt.SetCommandPanelTaskMode(rt.Name('create'))
        p_names = command_parameters_str.split(',')
        nodes = get_nodes_by_promethean_names(p_names)
        # we only consider nodes that are visible (and are not hidden)
        nodes = [node for node in nodes if isVisible(node)]
        nodes = [node for node in nodes if not node.isHidden]
        for node in nodes:
            set_as_dynamic_object(node)
        static_nodes = get_potential_static_nodes(nodes)
        # Important, otherwise some operations such as our convex test will fail
        for static_node in static_nodes:
            set_as_static_object(static_node)
        _simulated_nodes = nodes + static_nodes

    elif command == 'start_simulation':
        # setup MassFx
        physx_panel = rt.PhysXPanelInterface.instance
        if physx_panel:
            # TODO: Those settings should be editable by the user?
            physx_panel.enableGravity = True
            physx_panel.gravityMode = 2  # directional gravity
            physx_panel.gravityDirection = 3  # Z axis
            physx_panel.useGroundPlane = True
            # TODO: We should setup this value based on current scene units
            physx_panel.gravity = -981.0
            physx_panel.substeps = 5
            physx_panel.useMultiThread = False

        rt.macros.run('PhysX', 'PxResetSimMS')
        rt.macros.run('PhysX', 'PxPlaySimMS')

    elif command == 'cancel_simulation':
        rt.macros.run('PhysX', 'PxResetSimMS')
        for simulated_node in _simulated_nodes:
            massfx_modifier = get_massfx_modifier(simulated_node)
            if not massfx_modifier:
                continue
            rt.deleteModifier(simulated_node, massfx_modifier)
        _simulated_nodes = list()
        # we need to update xrefs to refresh vertex color display
        update_xrefs()

    elif command == 'end_simulation':
        if rt.nvpx.IsSimulating():
            rt.macros.run('PhysX', 'PxPlaySimMS')
        for simulated_node in _simulated_nodes:
            massfx_modifier = get_massfx_modifier(simulated_node)
            if not massfx_modifier:
                continue
            rt.deleteModifier(simulated_node, massfx_modifier)
        _simulated_nodes = list()
        # we need to update xrefs to refresh vertex color display
        update_xrefs()

    elif command == 'toggle_surface_snapping':
        rt.PlacementTool.ActiveMode = not rt.PlacementTool.ActiveMode
        rt.PlacementTool.UseBase = True  # make sure we snap to bottom of the meshes

        # global _snap_timer
        # _snap_timer.stop() if _snap_timer.isActive() else _snap_timer.start()

    elif command == 'get_camera_info':
        camera_info = get_camera_info()
        if camera_info:
            msg = camera_info

    else:  # pass through - standalone sends actual DCC code
        execute(command_str)
    if msg != 'DefaultValue':  # sometimes functions return a None and we need to communicate it back
        msg = msg or 'None'  # sockets won't send empty messages so sending 'None' as a string
        if type(msg) != str:
            msg = json.dumps(msg)
        return msg


def update_viewports():
    rt.redrawViews()


def set_current_units_multiplier():
    from pymxs import runtime as rt
    units_multiplier = 1.0
    unit_system = rt.units.SystemType
    units_dict = {'centimeters': 1.0,
                  'meters': 100.0,
                  'millimeters': 0.1,
                  'inches': 2.54,
                  'feet': 30.48,
                  'kilometers': 100000.0,
                  'miles': 160934.0}
    for unit in units_dict:
        if unit_system == rt.Name(unit):
            #
            units_multiplier = units_dict[unit] * rt.units.SystemScale
            break

    # We have to use this weird way of setting the global variable as this function is executed in some other 3ds Max
    # scope from MaxScript
    import promethean_3dsmax
    promethean_3dsmax.units_multiplier = units_multiplier
    print('Set current units multiplier to %s' % units_multiplier)


# Yes, we get the actual text of the previous function and add the invocation of it as the last line
code = inspect.getsource(set_current_units_multiplier) + 'set_current_units_multiplier()'
rt.callbacks.removeScripts(rt.Name('unitsChange'), id=rt.Name('PrometheanCallbacks'))
# We have to execute python raw code from the MaxScript as callbacks doesn't support python functions
rt.callbacks.addScript(rt.Name('unitsChange'), 'python.execute "%s"' % code, id=rt.Name('PrometheanCallbacks'),
                       persistent=False)

# call it for the first time to setup
set_current_units_multiplier()


# =====================================================================
# +++ 3DS MAX ADD FUNCTIONS
# =====================================================================
def add_object_by_path(path):
    # If we pass the object name, we just find the that object
    # if re.match('\w*_\d*$', path):
    #     existing_node = get_node_by_promethean_name(path)
    #     new_node = rt.copy(existing_node)
    #     new_node.transform = rt.Matrix3(1)
    # else:
    # we use asset name as the name of the object inside XRef
    asset_name = os.path.splitext(os.path.basename(path))[0]

    # Try to find existing XRef
    for xref_object in rt.getClassInstances(rt.XRefObject):
        if os.path.normcase(xref_object.fileName) == os.path.normcase(path):
            actual_nodes = rt.refs.dependentNodes(xref_object)
            # should always be true, but just in case
            if actual_nodes:
                new_node = rt.instance(actual_nodes[0])
                new_node.transform = rt.Matrix3(1)
                new_node.showVertexColors = actual_nodes[0].showVertexColors
                break
    else:
        # Create a new entry
        new_record = rt.objXRefMgr.AddXRefItemsFromFile(path, promptObjNames=False,
                                                        xrefOptions=rt.Name('localControllers'))
        # We need to retrieve the nodes from newly created record
        new_nodes = []
        nodes_to_delete = []
        for i in range(new_record.ItemCount(rt.Name('XRefObjectType'))):
            record_node = rt.refs.dependentNodes(new_record.GetItem(i+1, rt.Name('XRefObjectType')))[0]
            # FIXME: temporary fix for the basic assets
            if record_node.name.startswith('Bounding_Box_Kil'):
                nodes_to_delete.append(record_node)
            else:
                new_nodes.append(record_node)
        if nodes_to_delete:
            rt.delete(nodes_to_delete)
        # TODO: need to return all the top nodes
        new_node = new_nodes[0]
    return new_node


def set_mesh(mesh_path, nodes):
    global old_nodes_names_dict
    for node in nodes:
        if rt.classOf(node) == rt.XRefObject:
            if node.fileName == mesh_path:
                continue
        transform = node.transform
        new_node = add_object_by_path(mesh_path)
        new_node.transform = transform
        old_nodes_names_dict[node_to_promethean_name(node)] = new_node
        rt.delete(node)


def add_meshes_on_selection(meshes_paths):
    new_meshes = []
    for mesh_path in meshes_paths:
        new_meshes += add_mesh_on_selection(mesh_path)
    rt.select(new_meshes)


def add_mesh_on_selection(mesh_path):
    selection = rt.GetCurrentSelection()

    new_meshes = []
    for selected_object in selection:
        new_mesh = add_object_by_path(mesh_path)
        rt.move(new_mesh, selected_object.pos)
        rt.rotate(new_mesh, selected_object.rotation)
        rt.scale(new_mesh, selected_object.scale)
        new_meshes.append(new_mesh)
    return new_meshes


# add_objects {"Group111": {"group": true, "name": "Group111", "location": [0,1,2], "rotation":[3,4,5], "scale":[1,1,1]}}
# add_objects {"NewMesh111": {"group": false, "name": "NewMesh111", "location": [10,11,12], "rotation":[3,4,5], "scale":[1,2,3]}}
def add_object(obj_dict):
    if obj_dict.get('group', False):
        new_obj = rt.Point()
        new_obj.name = obj_dict['name']
        new_obj.cross = True
        new_obj.size = obj_dict['scale'][0] * rt.units.decodeValue('50cm')
    else:
        if obj_dict.get("asset_path", None):  # - reference asset by path
            new_obj = add_object_by_path(obj_dict["asset_path"])
            if not new_obj:
                print("Couldn't load the asset %s" % obj_dict["asset_path"])
                return None
            new_obj.scale = rt.Point3(*obj_dict['scale'])
        else:  # - create bounding box
            default_size = rt.units.decodeValue('100cm') / units_multiplier
            new_obj = rt.Box(length=default_size, width=default_size, height=default_size)
            # setting pivot to center to match default cube in Maya and UE4
            new_obj.pivot = rt.Point3(0, 0, default_size * 0.5)
            new_obj.name = obj_dict['name']
            with coordinate_system('gimbal'):
                new_obj.scale = rt.Point3(*obj_dict['scale'])

    with coordinate_system('gimbal'):
        new_obj.rotation = rt.EulerAngles(*obj_dict['rotation'])
    new_obj.position = rt.Point3(*obj_dict['location']) / units_multiplier
    parent_name = obj_dict.get('parent_dcc_name', None)
    if parent_name:
        parent_node = get_node_by_promethean_name(parent_name)
        if parent_node:
            parent(parent_node, new_obj)
        else:
            print('Parent to attach was not found: %s' % parent_name)
    return new_obj


def get_reference_path(node):
    return node.fileName if rt.classOf(node) == rt.XRefObject else None


# add_objects_from_polygons {"name": "FixedFurnitureCoatCloset", "points": [[0.0, 0.0, 0.0], [103.69, 0.0, 0.0], [103.69, 0.0, 60.0], [0.0, 0.0, 60.0]], "transform": {"translation": [937.9074, 0.0, 192.8589], "rotation": [0,0,0], "scale": [1,1,1]}}
# add_objects_from_polygons {"name": "FixedFurnitureCoatCloset", "points": [[0.0, 0.0, 0.0], [103.69, 0.0, 0.0], [103.69, 0.0, 60.0], [0.0, 0.0, 60.0]]}
def add_objects_from_polygons(geometry_list):
    """ each item in the list is a dictionary that stores a polygon based object
        TODO: a single object is currently one polygon. Need multi-polygon objects
     {'name': 'FixedFurniture CoatCloset',
      'points': [(0.0, 0.0, 0.0),
                 (103.69, 0.0, 0.0),
                 (103.69, 0.0, 60.0),
                 (0.0, 0.0, 60.0)],
      'transform': { 'translation': [0,0,0], 'rotation': [0,0,0], 'scale': [0,0,0] } """

    for geometry_dict in geometry_list:
        vert_array = geometry_dict["points"]

        new_mesh = build_mesh_from_verts(geometry_dict["name"], vert_array)

        if 'transform' in geometry_dict:  # only furniture has a transform so far
            position = geometry_dict['transform']['translation']
            rotation = geometry_dict['transform']['rotation']
            scale = geometry_dict['transform']['scale']
            setTransform(new_mesh, position, rotation, scale)


# add_objects_from_triangles {"obj1": {"name": "obj1", "verts": [[0.0,0.0,0.0], [1.0,0.0,0.0], [1.0,1.0,0.0], [0.0,1.0,0.0], [1.0,-1.0,0.0], [0.0,-1.0,0.0]], "tri_ids": [[0,1,2], [0,2,3], [0,5,1], [1,5,4]], "normals": [[0.0,1.0,0.0],[0.0,1.0,0.0],[0.0,1.0,0.0],[0.0,1.0,0.0]], "transform": {"translation": [0.0, 0.0, 0.0], "rotation": [15,30,45], "scale": [1,2,3]}}}
# add_objects_from_triangles {"obj1": {"name": "obj1", "verts": [[0.0,0.0,0.0], [1.0,0.0,0.0], [1.0,1.0,0.0], [0.0,1.0,0.0], [1.0,-1.0,0.0], [0.0,-1.0,0.0]], "tri_ids": [[0,1,2], [0,2,3], [0,1,5], [1,5,4]], "normals": [[0.0,1.0,0.0],[0.0,1.0,0.0],[0.0,1.0,0.0],[0.0,1.0,0.0]]}}
def add_objects_from_triangles(geometry_dicts):
    """ input is a dictionary with a unique dcc_name for a key and a dictionary that stores a triangle-based object value
    { dcc_name:
     {'name': 'FixedFurniture CoatCloset',
      'tri_ids': [(0, 1, 2),(0, 2, 3), (0, 1, 4) ... ],
      'verts': [(0.0, 0.0, 0.0), (103.69, 0.0, 0.0), (103.69, 0.0, 60.0), (), (), ... ], - unique vertexes
      'normals': [(0.0, 1.0, 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0), (), (), ... ], - normal per tri, matching order
      'transform': { 'translation': [0,0,0], 'rotation': [0,0,0], 'scale': [0,0,0] },
      dcc_name: {}
    } """
    out_names = {}
    for dcc_name in geometry_dicts:
        geometry_dict = geometry_dicts[dcc_name]
        temp_new_objects = []
        unique_verts = geometry_dict['verts']
        print('Constructing %s' % geometry_dict['name'])
        for i, tri_vert_id in enumerate(geometry_dict['tri_ids']):
            tri = [toPoint3(unique_verts[tri_vert_id[0]]), toPoint3(unique_verts[tri_vert_id[1]]),
                   toPoint3(unique_verts[tri_vert_id[2]])]
            tri = check_winding_order(tri, geometry_dict['normals'][i])
            temp_new_objects.append(polyCreateFacet(tri))
        new_mesh = polyUnite(geometry_dict['name'], temp_new_objects)
        rt.meshop.weldVertsByThreshold(new_mesh, new_mesh.verts, 0.1)  # merge verts with threshold distance at 0.1
        rt.CenterPivot(new_mesh)
        if 'transform' in geometry_dict:  # only furniture has a transform so far
            position = geometry_dict['transform']['translation']
            rotation = geometry_dict['transform']['rotation']
            scale = geometry_dict['transform']['scale']
            setTransform(new_mesh, position, rotation, scale)

        out_names[dcc_name] = geometry_dict['name']
    return out_names


def remove_descendants_recursively(node, remove_top_node=False):
    # need reversed order to work correctly and not crash
    for child in reversed(list(node.children)):
        remove_descendants_recursively(child, remove_top_node=True)
    if remove_top_node:
        rt.delete(node)


def check_winding_order(tri, normal):
    """ making sure triangles face the intended way """
    ab = [a - b for a, b in zip(tri[0], tri[1])]
    ac = [a - b for a, b in zip(tri[0], tri[2])]
    cross_product = cross(ab, ac)
    if dot(normal, cross_product) < 0:
        tri.reverse()
    return tri


def cross(a, b):
    """ cross product, maya doesn't have numpy """
    c = [a[1] * b[2] - a[2] * b[1],
         a[2] * b[0] - a[0] * b[2],
         a[0] * b[1] - a[1] * b[0]]
    return c


def dot(a, b):
    """ dimension agnostic dot product """
    return sum([x * y for x, y in zip(a, b)])


# =====================================================================
# +++ 3DS MAX LEARN FUNCTIONS
# =====================================================================

def learn_file(file_path, learn_file_path, extra_tags=[], project=None, from_selection=False):
    # [open file] cmds.file(file_path, open=1, force=1)  # will wait to open
    learn(learn_file_path, extra_tags=extra_tags, project=project, from_selection=from_selection)


def learn(file_path, extra_tags=[], project=None, from_selection=False):
    raw_data = getAllObjectsRawData(selection=from_selection)
    scene_id = rt.maxFileName + '/'
    learningCacheDataToFile(file_path, raw_data, scene_id, extra_tags, project)


def learningCacheDataToFile(file_path, raw_data, scene_id, extra_tags=[], project=None):
    learning_dict = {'raw_data': raw_data, 'scene_id': scene_id}
    if len(extra_tags) > 0:
        learning_dict['extra_tags'] = extra_tags
    if project is not None:
        learning_dict['project'] = project
    with open(file_path, 'w') as f:
        f.write(json.dumps(learning_dict))


def getRawObjectData(transform_node, predict_rotation=False, is_group=False):
    global units_multiplier
    transform_data = get_transform_data(transform_node)
    size, pivot = getTransform(transform_node)
    translation = [x * units_multiplier for x in transform_data['translation']]
    rotation = transform_data['rotation']
    scale = transform_data['scale']
    pivot_offset = transform_data['pivot_offset']
    transform = translation + rotation + scale  # WARNING! Instead of using a transform matrix we simplify to t,t,t,r,r,r,s,s,s
    parent_name = node_to_promethean_name(transform_node.parent) if transform_node.parent else 'no_parent'

    out_dict = {'raw_name': node_to_promethean_name(transform_node), 'parent_name': parent_name, 'is_group': is_group,
                'size': size, 'rotation': rotation, 'pivot': pivot, 'pivot_offset': pivot_offset, 'transform': transform}

    # - get art path
    art_asset_path = get_reference_path(transform_node)
    if art_asset_path:
        out_dict['art_asset_path'] = art_asset_path
    return out_dict


def getAllObjectsRawData(predict_rotation=False, selection=False):
    transforms = rt.getCurrentSelection() if selection else list(rt.objects)  # all objects
    semantic_group_class = rt.Helper  # Point
    transforms = [x for x in transforms if rt.superClassOf(x) in [rt.GeometryClass, semantic_group_class]]
    object_data_array = []
    for transform in transforms:
        if '_kill_' not in transform.name:
            is_group = rt.superClassOf(transform) != rt.GeometryClass
            obj_data = getRawObjectData(transform, predict_rotation=predict_rotation, is_group=is_group)
            object_data_array.append(obj_data)

    return object_data_array


def getPivot(bbox):
    # pivot is at the center of bounding box on XY and is min on Z
    bbox_min = bbox[0]
    bbox_max = bbox[1]
    return [bbox_min[0] + (bbox_max[0] - bbox_min[0]) / 2, bbox_min[1] + (bbox_max[1] - bbox_min[1]) / 2,
            bbox_min[2]]


def getSize(bbox):
    bbox_min = bbox[0]
    bbox_max = bbox[1]
    return [bbox_max[0] - bbox_min[0], bbox_max[1] - bbox_min[1], bbox_max[2] - bbox_min[2]]


def getTransform(node):
    global units_multiplier

    # check the bounding box in local coordinate system
    bbox = rt.nodeGetBoundingBox(node, node.transform)

    # get the pivot offset first as the bottom center
    pivot_offset = [x for x in getPivot(bbox)]
    # transform pivot offset to the object coordinates to get the WS pivot
    pivot_matrix = rt.transMatrix(rt.Point3(*pivot_offset))
    rt.rotate(pivot_matrix, rt.inverse(node.rotation))
    pivot = [a + b * c * units_multiplier for a, b, c in zip(node.pos, pivot_matrix.translationpart, node.scale)]

    # simply the size of the object
    size = [x * units_multiplier * scale  for x, scale in zip(getSize(bbox), node.scale)]

    return size, pivot


def get_triangle_positions(node):
    rt.convertToMesh(node)  # only works with meshes
    num_faces = rt.getNumFaces(node)
    out_verts = []
    for face_id in range(num_faces):
        out_verts += get_face_verts(node, face_id + 1)  # face ids start with 1 apparently
    rt.convertToPoly(node)  # convert to poly at the end
    return out_verts


def get_face_verts(node, face_id):
    vertex_indexes = rt.getFace(node, face_id)
    return [list(rt.getVert(node, vertex_index)) for vertex_index in vertex_indexes]


# =====================================================================
# +++ 3DS MAX MISC FUNCTIONS
# =====================================================================

def dump(text):
    try:
        with pymxs.mxstoken():
            pymxs.print_(str(text) + "\n", False, True)
    except Exception:
        pass


def execute(command):
    dump("PrometheanAI: Execute command '%s' " % command)
    try:
        with pymxs.mxstoken():
            result = rt.execute(command)
    except Exception as e:
        result = "Exception Command: " + e.message + "\n"
    return result


def nodes_to_promethean_names(nodes):
    # Promethean name format: object_name#ID
    # where ID is the 3ds Max handle
    return [node_to_promethean_name(node) for node in nodes]


def node_to_promethean_name(node):
    return '%s#%s' % (node.name, node.inode.handle)


def get_nodes_by_promethean_names(names, only_valid=False):
    nodes = [get_node_by_promethean_name(name) for name in names]
    return nodes if not only_valid else [node for node in nodes if node]


def get_node_by_promethean_name(name):
    # Promethean name format: object_name#ID
    # where ID is the 3ds Max handle

    # First try to retrieve name from some old name in case Promethean doesn't know the new object name
    global old_nodes_names_dict
    # renamed_node = old_nodes_names_dict.get(name, None)
    # if renamed_node:
    #     if rt.isValidNode(renamed_node):
    #         print('New node was found for the old name %s: %s' % (name, renamed_node))
    #         return renamed_node
    #     else:
    #         old_nodes_names_dict.pop(name)
    #         print('Removed node was found')

    # Try to get the node by the ID
    try:
        handle = int(name.rpartition('#')[-1])
        node = rt.maxOps.getNodeByHandle(handle)
        if node:
            return node
    except:
        pass

    print('No node found: %s' % name)
    return None


def get_geometry_in_view():
    # TODO: use some limit for performance?
    objs_in_view = []

    for obj in rt.geometry:
        if isVisible(obj):
            objs_in_view.append(obj)

    return objs_in_view


def parent(parent_node, children_nodes):
    if type(children_nodes) != list:
        children_nodes = [children_nodes]
    for node in children_nodes:
        # TODO:proper check if parent is not an ancestor of child
        node.parent = parent_node


def isVisible(obj):
    # TODO: need a better way of getting if the object on the screen

    # Check if center is in the camera
    if cameraCull(obj.center):
        return True
    # Check if any of the corners are in the camera
    # Get the list of all the points of the bounding box
    max = obj.max
    min = obj.min
    bbox_point_list = itertools.product(*[[min.x, max.x], [min.y, max.y], [min.z, max.z]])
    return any(cameraCull(rt.Point3(*point)) for point in bbox_point_list)


def cameraCull(pos):
    p = pos * rt.viewport.getTM()
    viewSize = rt.getViewSize()

    start = rt.mapScreenToView(rt.Point2(0, 0), p[2])
    end = rt.mapScreenToView(rt.Point2(viewSize[0], viewSize[1]), p[2])
    norm = start - end
    s = rt.Point2((rt.renderWidth / abs(norm.x)) * (p.x - start.x), - (rt.renderHeight / abs(norm.y)) * (p.y - start.y))

    if s[0] > 0 and s[1] > 0 and s[0] < rt.renderWidth and s[1] < rt.renderHeight:
        return True
    else:
        return False


def get_parent_list(node):
    parent_list = []
    if node.parent is not None:
        parent_list.append(node.parent)
        parent_list.extend(get_parent_list(node.parent))
        parent_list.reverse()
    return parent_list


def polyTriangulate(node):
    if node is not None:
        rt.convertToPoly(node)
        rt.select(node)
        execute("max modify mode")
        # triMod = rt.Edit_Poly()
        # triMod.name = "Triangulate"
        # rt.addModifier(obj, triMod)
        # rt.modPanel.setCurrentObject(triMod)
        rt.subObjectLevel = 1
        execute("actionMan.executeAction 0 \"40021\"")
        execute("$.EditablePoly.ConnectVertices ()")
        # triMod.ButtonOp("ConnectVertices")
        rt.deselect(node)
        return True
    else:
        return False


def get_transform_data(node):
    global units_multiplier
    translation = list(node.transform.translation)
    quat_rotation = node.transform.rotation
    euler_rotation = rt.quatToEuler(quat_rotation)
    rotation = [euler_rotation.x, euler_rotation.y, euler_rotation.z]
    scale = list(node.transform.scale)

    # get the pivot offset as the bottom center in the LS coordinates (no scaling or rotation)
    bbox = rt.nodeGetBoundingBox(node, node.transform)
    pivot_offset = [-x * units_multiplier for x in getPivot(bbox)]
    return {'translation': translation, 'rotation': rotation, 'scale': scale, 'pivot_offset': pivot_offset}


def toPoint3(any_array):
    return rt.Point3(any_array[0], any_array[1], any_array[2])


def polyUnite(name, nodes):
    final_node = nodes[0]
    final_node.name = name
    for i in range(1, len(nodes)):
        rt.meshop.attach(final_node, nodes[i])
    return final_node


def polyCreateFacet(tri):
    mesh = rt.editable_mesh()
    rt.meshop.setNumVerts(mesh, 3)
    rt.meshop.setvert(mesh, [1, 2, 3], tri)
    rt.meshop.createPolygon(mesh, [1, 2, 3])
    return mesh


def build_mesh_from_verts(name, vert_array, face_verts=4):
    vert_count = len(vert_array)
    rt.execute("tmp_mesh = mesh numverts:" + str(vert_count) + " name:\"" + name + "\"")
    for v in range(0, vert_count):
        vert = vert_array[v]
        rt.meshop.setvert(rt.tmp_mesh, v + 1, rt.Point3(vert[0], vert[1], vert[2]))

    for v in range(0, vert_count / face_verts, face_verts):
        if face_verts == 4:
            face_indx = [v + 1, v + 2, v + 3, v + 4]
        else:
            face_indx = [v + 1, v + 2, v + 3]
        rt.meshop.createPolygon(rt.tmp_mesh, face_indx)

    return rt.tmp_mesh


def setTransform(mesh, position, rotation, scale):
    mesh.position = rt.Point3(position[0], position[1], position[2])
    mesh.rotation = rt.eulerangles(rotation[0], rotation[1], rotation[2])
    mesh.scale = rt.Point3(scale[0], scale[1], scale[2])


def unparent(nodes):
    for node in nodes:
        node.parent = None


def set_vertex_color(color=None, channel=0):
    sel = rt.GetCurrentSelection()
    if not sel:
        return

    with undo(True, 'Setting vertex colors'):
        rt.DisableSceneRedraw()
        try:
            # 	channels --> 0: color; 1: illum; 2: alpha
            if channel == 1:
                vc_type = 'color'
            elif channel == 2:
                vc_type = 'illum'
            else:
                vc_type = 'color'

            if color is None:
                color = [255, 255, 255]
            mx_color = rt.Point3(color[0], color[1], color[2])

            for obj in sel:
                obj.showVertexColors = True
                obj.vertexColorType = vc_type
                curr_mod = rt.modPanel.getCurrentObject()
                if rt.classOf(curr_mod) not in [rt.Edit_Poly, rt.Editable_Poly]:
                    curr_mod = rt.Edit_Poly()
                    rt.addModifier(obj, curr_mod)

                sel_faces = curr_mod.GetSelection('face')
                if sel_faces:
                    rt.polyop.SetFaceColor(obj, channel, sel_faces, mx_color)
                else:
                    curr_mod.ConvertSelection('object', 'vertex')
                    sel_verts = curr_mod.GetSelection('vertex')
                    rt.polyop.setvertcolor(obj, channel, sel_verts, mx_color)

        finally:
            rt.EnableSceneRedraw()
            rt.CompleteRedraw()


def set_uv_quadrant(u, v):
    # TODO: make sure it works for the whole meshes
    sel = rt.getCurrentSelection()
    if not sel:
        return

    for node in sel:
        curr_mod = rt.modPanel.getCurrentObject()

        if rt.classOf(curr_mod) != rt.Unwrap_UVW:
            unwrap_mode = rt.Unwrap_UVW()
            rt.addModifier(node, unwrap_mode)
        else:
            unwrap_mode = curr_mod

        current_panel = rt.GetCommandPanelTaskMode()
        if current_panel != rt.Name('modify'):
            rt.SetCommandPanelTaskMode(rt.Name('modify'))

        # Separate selection to new UV element
        unwrap_mode.breakSelected()

        selected_faces = unwrap_mode.getSelectedFaces()
        # We'll go through the UV elements and move them one by one, cause some of them can have an offset
        # So we store already used faces for identifying those elements
        moved_faces = rt.BitArray()
        moved_faces.count = selected_faces.count
        for i, face in enumerate(selected_faces):
            # If selected
            if face:
                # If not yet moved
                if not moved_faces[i]:
                    # Select current face
                    one_face_selection = rt.BitArray()
                    one_face_selection.count = selected_faces.count
                    one_face_selection[i] = True
                    unwrap_mode.selectFaces(one_face_selection)
                    # Expand to the element and select
                    unwrap_mode.selectElement()
                    element_faces = unwrap_mode.getSelectedFaces()
                    # Mark faces as moved
                    moved_faces = rt.join(moved_faces, element_faces)
                    # Move the whole shell to the needed quad
                    element_position = unwrap_mode.getSelCenter()
                    offset_u = element_position[0] // 1
                    offset_v = element_position[1] // 1
                    uv_pos = rt.Point3(u-offset_u, v-offset_v, 0)
                    unwrap_mode.moveSelected(uv_pos)
        # Select original selection back
        rt.convertTo(node, rt.PolyMeshObject)
        rt.polyop.setFaceSelection(node, selected_faces)
        rt.setSelectionLevel(node, rt.Name('Face'))


def get_vertex_colors(nodes=None):
    nodes = nodes or rt.GetCurrentSelection()
    if not nodes:
        return

    curr_mod = rt.modPanel.getCurrentObject()

    vertex_colors_data = dict()
    for obj in nodes:
        vertex_data = set()
        vertex_colors = set()
        temp_mesh = rt.snapshotasmesh(obj)
        vertex_colors_total = rt.getNumCPVVerts(temp_mesh)
        if vertex_colors_total <= 0:
            continue
        # Loop through all faces
        for face in range(1, rt.meshop.getNumFaces(temp_mesh) + 1):
            # Get every index of the vertex on the face
            for vert_i in range(3):
                # Color-Per-Vertex Vertex index here
                c_vert_index = rt.meshop.getMapFace(temp_mesh, 0, face)[vert_i]
                # Getting the color
                vertex_color = rt.getVertColor(temp_mesh, c_vert_index)
                # Only checking if the color exist, assuming that roughness, metalness and texture parameters are the same
                # We may change this in the future if find a quick way to select appropriate faces fast in select_vertex_color function
                if vertex_color not in vertex_colors:
                    t_vert_index = rt.meshop.getMapFace(temp_mesh, 1, face)[vert_i]
                    uv_coord = rt.getTVert(temp_mesh, t_vert_index)
                    roughness = math.floor(uv_coord.x + 5) / 10.0
                    v = uv_coord.y
                    has_texture = 0 <= v <= 1
                    metalness = False if has_texture else v < -1
                    vertex_colors.add(vertex_color)
                    vertex_data.add((vertex_color.red, vertex_color.green, vertex_color.blue, vertex_color.alpha, roughness, metalness, has_texture))
        if vertex_data:
            vertex_colors_data[str(obj.handle)] = list(vertex_data)
    return vertex_colors_data


def select_vertex_color(nodes, color):
    nodes = nodes or rt.GetCurrentSelection()
    if not nodes:
        return

    # TODO: Add support for multiple nodes
    node = nodes[0]
    if not node:
        return

    target_color = color[:-1]

    current_panel = rt.GetCommandPanelTaskMode()
    if current_panel != rt.Name('modify'):
        rt.SetCommandPanelTaskMode(rt.Name('modify'))

    curr_mod = rt.modPanel.getCurrentObject()
    if rt.classOf(curr_mod) not in [rt.Edit_Poly, rt.Editable_Poly]:
        rt.convertTo(node, rt.PolyMeshObject)


    # getting vertexes mask by color
    verts_by_color = rt.polyop.getVertsByColor(node, rt.Color(*target_color), 1, 1, 1)
    # getting vace mask of all the faces around those vertexes
    faces_bit = rt.polyop.getFacesUsingVert(node, verts_by_color)
    for face, included in enumerate(faces_bit):
        if included:
            # We consider that the face has the same color on its all vertexes, so we check only the first
            c_vert_index = rt.polyop.getMapFace(node, 0, face + 1)[0]
            # Getting the vertex color
            vertex_color = node.getMapVertex(0, c_vert_index)
            # Compare per component to early exit if the color is not the same
            faces_bit[face] = all(int(channel * 255) == target_color[i] for i, channel in enumerate(vertex_color))
    # Distinguish between Edit_Poly and Editable_Poly
    if rt.classOf(curr_mod) == rt.Edit_Poly:
        curr_mod.SetEPolySelLevel(rt.Name('Face'))
        curr_mod.SetSelection(rt.Name('Face'), faces_bit)
    else:
        rt.setSelectionLevel(node, rt.Name('Face'))
        node.SetSelection(rt.Name('Face'), faces_bit)


def set_roughness(value):
    # -5 < U < 5

    # We convert the 0 (no roughness) to 1 (full roughness) value to the proper mapping (-5 to 5)
    new_value = int((float(value) * 10) - 5)

    set_uv_quadrant(new_value, None)


def set_metallic(is_metalic, has_texture=False):
    # V < -1 is metal
    # -1 < V < 0 is not metal and not texture
    # V > 0 is texture

    v = -2 if is_metalic else 0 if has_texture else -1
    set_uv_quadrant(None, v)


def set_texture_tiling(has_texture, is_metalic=False):
    # V < -1 is metal
    # -1 < V < 0 is not metal and not texture
    # V > 0 is texture

    v = 0 if has_texture else -2 if is_metalic else -1
    set_uv_quadrant(None, v)


@contextlib.contextmanager
def undo(enabled=True, message=""):
    exc = None
    with pymxs.undo(enabled, message):
        try:
            yield
        except Exception as exc:
            pass
    if exc:
        raise exc


# =====================================================================
# +++ TRANSFORMATIONS
# =====================================================================
def translate(translation, node):
    # move in world
    node.pos = rt.Point3(*translation)


def scale(scale_value, node):
    # scale locally
    with coordinate_system('gimbal'):
        node.scale = rt.Point3(*scale_value)


def rotate(rotation, node):
    # rotate locally
    with coordinate_system('gimbal'):
        node.rotation = rt.EulerAngles(*rotation)


def translate_relative(translation, node):
    # move in world
    node.pos = node.pos + rt.Point3(*translation)


def scale_relative(scale_value, node):
    # scale locally
    with coordinate_system('local'):
        node.scale = rt.Point3(*scale_value)


def rotate_relative(rotation, node):
    # rotate locally
    with coordinate_system('local'):
        node.rotation = rt.EulerAngles(*rotation)


@contextlib.contextmanager
def coordinate_system(system_name):
    """
    Allows to make transformations in the given coordinate system and return back the system that was set originally
    :param system_name: can be 'local', 'parent', 'world', etc. Default is 'default'
    """
    # TODO: check if this function may break standard 3ds Max rotation tool for rotated objects
    context = getattr(pymxs.runtime, '%coordsys_context')
    new_coordinate_system = rt.Name(system_name)
    prev_coordinate_system = context(new_coordinate_system, None)
    try:
        yield
    except Exception as e:
        context(rt.Name('default'), None)
        raise e
    finally:
        context(prev_coordinate_system, None)

def get_all_descendents(node):
    """
    Creates a generator listing all the children of the node
    """
    for child in node.children:
        sub_children = child.children
        if sub_children:
            for sub in get_all_descendents(child):
                yield sub
        yield child

# =====================================================================
# +++ XREFS
# =====================================================================
def create_asset_from_selection(root_path):
    """
    Export selected node to a separate .max file, swap it and provides node with XRef to that file
    """
    # TODO: make it work with multiple selected nodes
    selection = rt.GetCurrentSelection()
    exported_paths = []
    if selection:
        top_transforms = []
        # Make sure we only have transforms that have any mesh in descendents
        selection_with_meshes_under = [node for node in selection if rt.superClassOf(node) == rt.GeometryClass
                                       or rt.classOf(node) == rt.Dummy and any(rt.superClassOf(x) == rt.GeometryClass for x in get_all_descendents(node))]
        # Identify top transforms to be used for export
        all_children = []
        [all_children.extend(get_all_descendents(node)) for node in selection]
        top_transforms = [node for node in selection_with_meshes_under if not node in all_children]
        if top_transforms:
            max_scene_name = rt.maxFileName
            for top_transform in top_transforms:
                QApplication.processEvents()
                if rt.classof(top_transform) != rt.XRefObject:
                    transform = top_transform.transform
                    name = top_transform.name
                    # remove digits from the end of the name as indicator of the copy number
                    final_name = re.sub(r'\d{3,4}$', '', name)
                    # remove all non english letters and translate what we can
                    import unicodedata
                    final_name = unicodedata.normalize('NFKD', final_name).encode('ascii', 'ignore')
                    # remove the rest non valid symbols
                    final_name = get_valid_file_name(final_name)

                    # Make sure we don't override existing file, cause the names may clash
                    import hashlib
                    hash_directory = os.path.dirname(max_scene_name)
                    path_hash = hashlib.md5(hash_directory.encode()).hexdigest()  # unique hash folder per maya file
                    export_file_path = os.path.join(root_path, path_hash, '%s.max' % final_name)
                    export_file_path = get_unique_file_path(export_file_path)
                    # - create folders
                    export_dir = os.path.dirname(export_file_path)
                    if not os.path.exists(export_dir):
                        os.makedirs(export_dir)
                    # Reset the transform before exporting to restore later
                    # top_transform.name = final_name
                    top_transform.transform = rt.Matrix3(1)
                    if rt.saveNodes([top_transform] + list(get_all_descendents(top_transform)), export_file_path, quiet=True):
                        print('File saved: %s' % export_file_path)
                        exported_paths.append(export_file_path)
                        # # Create a new entry
                        # new_record = rt.objXRefMgr.AddXRefItemsFromFile(export_file_path, promptObjNames=False, xrefOptions=rt.Name('localControllers'))
                        # if new_record:
                        #     # We need to retrieve the node from newly created record
                        #     xref_node = rt.refs.dependentNodes(new_record.GetItem(1, rt.Name('XRefObjectType')))[0]
                        #     if xref_node:
                        #         # N.B.! There is a weird bug that Max reset's the xref node's position, but keep it for instances
                        #         # so we have to swap the original node with the instance and remove the xref later
                        #         instance_node = rt.instanceReplace(top_transform, xref_node)
                        #         instance_node.transform = transform
                        #         print('XRef created: %s' % instance_node.name)
                        #         rt.delete(xref_node)
                        #         new_nodes[instance_node] = export_file_path
                        # else:
                        #     print('Failed to reference from %s with name %s' % (export_file_path, final_name))
                    else:
                        print('Failed to export node %s to %s' % (top_transform.name, export_file_path))
                    top_transform.transform = transform
                else:
                    print('Selected node is already XRef')
    return exported_paths


def get_valid_file_name(text):
    return(''.join([x if 0 <= ord(x) <= 127 and (x.isalnum() or x in [' ', '-']) else '_' for x in text]).replace(' ', '_')).lower()


# =====================================================================
# +++ 3ds Max Raytrace
# =====================================================================

def raytrace(start_point, direction, distance=9999999999, ignore_nodes=()):
    test_ray = rt.ray(start_point, direction)
    result = rt.intersectRayScene(test_ray)
    closest_hit_distance = None
    closest_hit_node = None
    closest_hit_ray = None

    for hit in result:
        hit_node, hit_ray = hit
        hit_distance = rt.length(start_point - hit_ray.pos)
        if (closest_hit_distance is None or hit_distance < closest_hit_distance) and hit_distance <= distance and not hit_node in ignore_nodes:
            closest_hit_distance = hit_distance
            closest_hit_node = hit_node
            closest_hit_ray = hit_ray

    if not closest_hit_distance:
        return None, None, None

    return closest_hit_node, list(closest_hit_ray.pos), list(closest_hit_ray.dir)


def find_close_intersection_point(node, direction, distance):
    # Raytrace both ways from the node position
    # - direction has magnitude
    # raytrace from the bottom center of the object
    _, pivot = getTransform(node)
    start_point = rt.Point3(*pivot)
    # TODO: raytrace both ways?
    return raytrace(start_point, direction, distance, ignore_nodes=[node])


def translate_and_raytrace_by_name(nodes, location, raytrace_distance, max_normal_deviation, ignore_nodes):
    start_point = location
    for node in nodes:
        if raytrace_distance:
            hit_object, hit_position, hit_normal = raytrace(rt.Point3(*start_point), rt.Point3(0, 0, -1), raytrace_distance, ignore_nodes=ignore_nodes)
            if hit_object:
                location = hit_position
                up_dot_product = dot([0, 0, 1], hit_normal)
                # If we pass the threshold, align the objects to the normal
                if up_dot_product > max_normal_deviation:
                    node.dir = rt.Point3(*hit_normal)
        # TODO: extract in a separate function and make a good refactoring
        ws_pivot_offset = node.pos - rt.Point3(*getTransform(node)[1])
        node.pos = rt.Point3(*location) + ws_pivot_offset


def snap_to_cursor():
    current_selection = list(rt.selection)
    if not current_selection:
        return

    viewport_under_mouse = get_viewport_under_mouse()
    if viewport_under_mouse is None:
        return

    # rt.viewport.activeViewport = viewport_under_mouse

    mouse_ray = rt.mapScreenToWorldRay(rt.mouse.pos)
    scene_intersections = rt.intersectRayScene(mouse_ray)
    scene_intersections = list(scene_intersections)
    world_pos = None
    for intersection in reversed(scene_intersections):
        intersect_node, intersect_ray = intersection
        if intersect_node in current_selection:
            continue
        world_pos = intersect_ray.pos
        break
    if not world_pos:
        world_pos = rt.mapScreenToCP(rt.mouse.pos)

    for node in current_selection:
        node.pos = world_pos


_snap_timer.timeout.connect(snap_to_cursor)


# =====================================================================
# +++ 3ds Max ASSET FUNCTIONS
# =====================================================================

def get_current_viewport():
    return rt.viewport.activeViewport


def get_viewports_data():

    viewports = OrderedDict()

    current_viewport = get_current_viewport()
    for i in range(rt.viewport.numViews):
        index = ((i + current_viewport) % rt.viewport.numViews) + 1
        rt.viewport.activeViewport = index
        viewport_top_left = rt.mouse.screenPos - rt.mouse.pos
        viewport_size = rt.getViewSize()
        viewports[index] = rt.Box2(viewport_top_left.x, viewport_top_left.y, viewport_size.x, viewport_size.y)

    return viewports


def get_viewport_under_mouse():
    viewports_data = get_viewports_data()
    if not viewports_data:
        return

    current_viewport_data = None
    current_viewport = get_current_viewport()
    if current_viewport != 0:
        current_viewport_data = viewports_data.get(current_viewport)
    if not current_viewport_data:
        return

    viewport_under_mouse = None
    for i in range(rt.viewport.numViews):
        viewport_index = i + 1
        viewport_data = viewports_data.get(viewport_index, None)
        if not viewport_data:
            continue
        if rt.contains(viewport_data, rt.mouse.screenPos):
            viewport_under_mouse = viewport_index
            break

    return viewport_under_mouse


def load_asset(file_path):
    # THIS IS A DROP ASSET AND SHOULD BE RENAME AS SUCH!
    if not file_path:
        return

    if file_path.endswith('__'):
        file_path = file_path[:-2]
    if not os.path.isfile(file_path):
        return

    viewport_under_mouse = get_viewport_under_mouse()
    if viewport_under_mouse is None:
        return

    rt.viewport.activeViewport = viewport_under_mouse

    mouse_ray = rt.mapScreenToWorldRay(rt.mouse.pos)
    closest_node, closest_position, closest_direction = raytrace(mouse_ray.pos, mouse_ray.dir)

    if closest_node:
        intersected_node_data = closest_node
        world_pos = rt.Point3(*closest_position)
    else:
        world_pos = rt.mapScreenToCP(rt.mouse.pos)

    new_node = add_object_by_path(file_path)
    ws_pivot_offset = new_node.pos - rt.Point3(*getTransform(new_node)[1])
    rt.move(new_node, world_pos + ws_pivot_offset)

    if closest_direction:
        closest_direction = [round(x, 2) for x in closest_direction]
        # if aligned all the way up don't random rotate!
        # if not aligned up fully sideways like walls, don't rotate too Z == 0 is fully sideways
        if not closest_direction[2] == 0 and not all(x == y for x, y in zip(closest_direction, [0, 0, 1])):
            new_node.dir = rt.Point3(*closest_direction)

    # TODO: This should be removed once our 3ds max base assets rotation lock status are fixed
    # we make sure rotations are unlocked
    # unlock rotation attributes in case locked
    # 4,5,6 is X,Y,Z rotation axes
    lock_flags = rt.getTransformLockFlags(new_node)
    lock_flags[3] = False
    lock_flags[4] = False
    lock_flags[5] = False
    rt.setTransformLockFlags(new_node, lock_flags)


# =====================================================================
# +++ 3DS MAX MASSFX
# =====================================================================

DEFAULT_STATIC_NODE_NAMES = ['floor', 'terrain']


def get_massfx_modifier(node):
    if not node:
        return False

    modifier_found = None
    for modifier in node.modifiers:
        if rt.classOf(modifier) == rt.MassFX_RBody:
            modifier_found = modifier
            break

    return modifier_found


def set_as_dynamic_object(node):
    if not node:
        return None

    massfx_modifier = get_massfx_modifier(node)
    if massfx_modifier:
        return None

    mass_modifier = rt.MassFX_RBody()
    rt.addModifier(node, mass_modifier)
    mass_modifier.type = 1  # dynamic
    mass_modifier.CollideWithRigidBodies = True
    mass_modifier.enableGravity = True

    return mass_modifier


def set_as_static_object(node):
    if not node:
        return None

    massfx_modifier = get_massfx_modifier(node)
    if massfx_modifier:
        return None

    mass_modifier = rt.MassFX_RBody()
    rt.addModifier(node, mass_modifier)
    mass_modifier.type = 3  # static
    mass_modifier.CollideWithRigidBodies = True
    mass_modifier.enableGravity = False
    mass_modifier.meshType = 5      # original
    mass_modifier.mass = 4.35491
    mass_modifier.meshCustomMesh = None
    mass_modifier.massCenterX = 0
    mass_modifier.massCenterY = 0
    mass_modifier.massCenterZ = 0

    return mass_modifier


def get_potential_static_nodes(dynamic_nodes):
    static_nodes = list()

    # 1) Nodes that are in the default list of static nodes
    dynamic_nodes = dynamic_nodes or list()
    for default_name in DEFAULT_STATIC_NODE_NAMES:
        node = rt.getNodeByName(default_name)
        if not node or node in dynamic_nodes:
            continue
        static_nodes.append(node)

    # 2) Nodes that are visible
    geo_in_view = [node for node in get_geometry_in_view() if node not in dynamic_nodes and node not in static_nodes]
    static_nodes.extend(geo_in_view)

    return static_nodes


def update_xrefs():
    xr = rt.objXRefMgr
    for i in range(1, xr.recordCount + 1):
        xro = xr.GetRecord(i)
        if not xro:
            continue
        xro.Update()


def get_unique_file_path(desired_path):
    """
    Finds the file path that doesn't exist yet in the same folder as the desired path
    :param desired_path: path that you'd use in case there are no files in the destination folder
    """
    if not os.path.exists(desired_path):
        return desired_path
    file_name, extension = os.path.splitext(os.path.basename(desired_path))
    folder = os.path.dirname(desired_path)
    new_file_path = ''
    i = 1
    while True:
        new_file_path = os.path.join(folder, '{}-{}{}'.format(file_name, i, extension))
        if not os.path.exists(new_file_path):
            break
        i += 1
    return os.path.normpath(new_file_path)


def get_camera_info():
    global units_multiplier
    if rt.viewport.IsPerspView():
        fov = rt.viewport.GetFOV()
        # get the transformation matrix for the camera
        view_to_world = rt.Inverse(rt.getViewTM())
        view_dir = list(-view_to_world.row3)
        view_pos = [x * units_multiplier for x in view_to_world.row4]
        info_dict = {'camera_location': view_pos, 'camera_direction': view_dir, 'fov': fov, 'objects_on_screen': nodes_to_promethean_names(get_geometry_in_view())}
        return info_dict
    return None