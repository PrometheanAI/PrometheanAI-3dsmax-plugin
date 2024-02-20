import os
import json
import time

from pymxs import runtime as rt

TEMP_FOLDER = r"C:\PrometheanAI_Temp"
FLOAT_PRECISION = 3

IKEA_UNITS_MULTIPLIER = 0.1  # mm
CURRENT_UNITS_MULTIPLIER = IKEA_UNITS_MULTIPLIER
# *********************************************************************
#  +++ FUNCTIONS
# *********************************************************************
def extract_max_scene_metadata(scene_path=None):
    # TODO make proper unit conversion based on the scene Units (no inches, pretty please.....)
    scene_path = scene_path or os.path.join(rt.maxFilePath, rt.maxFileName)
    scene_data_dict = {}
    scene_data_dict['name'] = os.path.splitext(os.path.basename(scene_path))[0]
    scene_data_dict['thumbnail'] = os.path.join(TEMP_FOLDER, scene_data_dict['name'] + '.png')

    scene_data_dict['path'] = scene_path   # use absolute
    scene_data_dict['type'] = 'mesh'
    scene_data_dict['date'] = os.path.getmtime(scene_path)  # last modified. could be by perforce
    # TODO
    # - get lod data
    scene_data_dict['lod_count'] = 1
    scene_data_dict['material_count'] = 0
    scene_data_dict['material_paths'] = []
    scene_data_dict['vertex_count'] = 0
    scene_data_dict['face_count'] = 0

    scene_data_dict['bounding_box'] = [abs(x) for x in convert_point(get_dimensions(rt.geometry, CURRENT_UNITS_MULTIPLIER))]
    scene_data_dict['pivot_offset'] = convert_point(get_pivot_offset(rt.geometry, CURRENT_UNITS_MULTIPLIER))
    # assume there is only one channel
    scene_data_dict['vertex_color_channels'] = 0
    scene_data_dict['uv_sets'] = 0
    for mesh in rt.geometry:
        # universal approach for Editable Poly and Mesh
        temp_mesh = rt.snapshotasmesh(mesh)
        for i in range(rt.meshOp.getNumMaps(temp_mesh)):
            # if actual info is there
            if rt.meshOp.getMapSupport(temp_mesh, i):
                # 0  is vertex color, all the others - uv channels
                if i == 0:
                    scene_data_dict['vertex_color_channels'] += 1
                else:
                    scene_data_dict['uv_sets'] += 1
        scene_data_dict['vertex_count'] += temp_mesh.numverts
        scene_data_dict['face_count'] += temp_mesh.numfaces
        rt.free(temp_mesh)
        if rt.classOf(mesh.material) == rt.Multimaterial:
            for material in mesh.material.materialList:
                # TODO paths(?)
                scene_data_dict['material_paths'].append(material.name)
                scene_data_dict['material_count'] += 1
        else:
            scene_data_dict['material_paths'].append(mesh.material.name)
            scene_data_dict['material_count'] += 1
    return scene_data_dict


def write_current_scene_metadata_to_file(scene_path=None, learning_cache_path=None):
    learning_cache_path = learning_cache_path or os.path.join(TEMP_FOLDER, 'learning_cache.json')
    try:
        existing_data = load_json_file(learning_cache_path)
    except:  # sometimes we read the file faster then it's finished being written to on the windows side
        time.sleep(3)
        existing_data = load_json_file(learning_cache_path)

    if not os.path.exists(TEMP_FOLDER):
        os.makedirs(TEMP_FOLDER)
    data = extract_max_scene_metadata(scene_path)  # do this before the file is open in case there is crash
    if data:
        with open(learning_cache_path, 'w') as f:
            existing_data[data['path']] = data  # add to existing file
            json.dump(existing_data, f, indent=4)


def load_json_file(file_path):
    # not using this function from promethean generic because this file has to be read by the maya interpreter
    # that will not find all the dependencies
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return json.load(f) or {}
    return {}


def convert_point(point):
    """ 3ds Max has an YZ axis swizzle """
    return [point[0], point[2], point[1]]


def get_dimensions(obj_name, unit_multiplier=1.0):
    return [round(x * unit_multiplier, FLOAT_PRECISION) for x in (obj_name.max - obj_name.min)]


def get_pivot_offset(obj_name, unit_multiplier=1.0):
    # offset is calculated as center of XY of bounding box and minZ
    center = (obj_name.min + obj_name.max) * unit_multiplier / 2
    return [-round(x, FLOAT_PRECISION) for x in [center.x, center.y, obj_name.min.z * unit_multiplier]]