import collections
import hashlib
import json
import re
import fnmatch
from enum import Enum
import os
from os import listdir, makedirs
from os.path import dirname, isfile, join, realpath
from datetime import date

import jsonschema
import yaml
from jinja2 import Environment, PackageLoader
from yaml import MarkedYAMLError

from binary import FixSizedEntryListTypes, FixSizedTypes, FixSizedListTypes, FixSizedMapTypes
from cpp import (
    cpp_ignore_service_list, 
    cpp_types_decode, 
    cpp_types_encode, 
    get_size, 
    is_trivial, 
    cpp_param_name
)
from cs import (
    cs_escape_keyword,
    cs_ignore_service_list,
    cs_types_decode,
    cs_types_encode,
    cs_custom_codec_param_name,
    cs_sizeof
)
from java import java_types_decode, java_types_encode
from md import internal_services
from py import (
    py_escape_keyword,
    py_get_import_path_holders,
    py_ignore_service_list,
    py_param_name,
    py_types_encode_decode,
    py_custom_type_name,
    py_decoder_requires_to_object_fn,
    py_to_object_fn_in_decode,
)
from ts import (
    ts_escape_keyword,
    ts_get_import_path_holders,
    ts_ignore_service_list,
    ts_types_decode,
    ts_types_encode,
)

MAJOR_VERSION_MULTIPLIER = 10000
MINOR_VERSION_MULTIPLIER = 100
PATCH_VERSION_MULTIPLIER = 1

ID_VALIDATOR_IGNORE_SET = {"Jet", "Experimental"}


def java_name(type_name):
    return "".join([capital(part) for part in type_name.split("_")])


def cs_name(type_name):
    return "".join(
        [capital(part) for part in type_name.replace("(", "").replace(")", "").split("_")]
    )


def cpp_name(type_name):
    return "".join(
        [capital(part) for part in type_name.replace("(", "").replace(")", "").split("_")]
    )


def param_name(type_name):
    return type_name[0].lower() + type_name[1:]


def is_fixed_type(param):
    return param["type"] in FixSizedTypes


def capital(txt):
    return txt[0].capitalize() + txt[1:]


def to_upper_snake_case(camel_case_str):
    return re.sub("((?<=[a-z0-9])[A-Z]|(?!^)[A-Z](?=[a-z]))", r"_\1", camel_case_str).upper()
    # s1 = re.sub('(.)([A-Z]+[a-z]+)', r'\1_\2', camel_case_str)
    # return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).upper()


def version_to_number(major, minor, patch=0):
    return (
        MAJOR_VERSION_MULTIPLIER * major
        + MINOR_VERSION_MULTIPLIER * minor
        + PATCH_VERSION_MULTIPLIER * patch
    )


def get_version_as_number(version):
    if not isinstance(version, str):
        version = str(version)
    return version_to_number(*map(int, version.split(".")))


def fixed_params(params):
    return [p for p in params if is_fixed_type(p)]


def var_size_params(params):
    return [p for p in params if not is_fixed_type(p)]


def new_params(since, params):
    """
    Returns the list of parameters that are added later than given version.
    Because the method should precede all the parameters that are added
    latter, a simple equality check between the versions that the method and
    the parameter is added is enough.
    """
    return [p for p in params if p["since"] != since]


def filter_new_params(params, version):
    """
    Returns the filtered list of parameters such that,
    the resulting list contains only the ones that are added
    before or at the same time with the given version.
    """
    version_as_number = get_version_as_number(version)
    return [p for p in params if version_as_number >= get_version_as_number(p["since"])]


def generate_data_containing_requests_lookup_table(services, custom_services):
    table = collections.defaultdict(dict)

    types_containing_serialized_data = {
        "Data",
    }

    types_not_containing_serialized_data = set()

    if not custom_services:
        custom_types = {}
    else:
        custom_types = {
            custom["name"]: custom
            for custom in custom_services[0]["customTypes"]
        }

    def type_contains_serialized_data(type_name):
        if type_name in types_containing_serialized_data:
            return True
        elif type_name in types_not_containing_serialized_data:
            return False
        elif type_name.startswith("List_") or type_name.startswith("ListCN_") or type_name.startswith("Set_"):
            item_type_name = type_name.split("_", 1)[1]
            if type_contains_serialized_data(item_type_name):
                types_containing_serialized_data.add(type_name)
                return True
        elif type_name.startswith("Map_") or type_name.startswith("EntryList_"):
            key_type_name, value_type_name = type_name.split("_", 2)[1:3]
            if type_contains_serialized_data(key_type_name) or type_contains_serialized_data(value_type_name):
                types_containing_serialized_data.add(type_name)
                return True
        elif type_name in custom_types:
            if custom_type_contains_serialized_data(type_name):
                types_containing_serialized_data.add(type_name)
                return True

        types_not_containing_serialized_data.add(type_name)
        return False

    def custom_type_contains_serialized_data(custom_type_name):
        custom_type = custom_types[custom_type_name]
        for param in custom_type["params"]:
            param_type_name = param["type"]
            if type_contains_serialized_data(param_type_name):
                return True

        return False

    for service in services:
        service_name = service["name"]
        service_table = table[service_name]
        for method in service["methods"]:
            method_name = method["name"]
            for param in method["request"].get("params", []):
                if type_contains_serialized_data(param["type"]):
                    service_table[method_name] = True
                    break
            else:
                service_table[method_name] = False

    return table


def generate_codecs(services, custom_services, template, output_dir, lang, env):
    makedirs(output_dir, exist_ok=True)

    data_containing_requests = generate_data_containing_requests_lookup_table(services, custom_services)

    id_fmt = "0x%02x%02x%02x"
    if lang is SupportedLanguages.CPP:
        curr_dir = dirname(realpath(__file__))
        cpp_dir = "%s/cpp" % curr_dir
        f = open(join(cpp_dir, "header_includes.txt"), "r")
        save_file(join(output_dir, "codecs.h"), f.read(), "w")
        f = open(join(cpp_dir, "source_header.txt"), "r")
        save_file(join(output_dir, "codecs.cpp"), f.read(), "w")

    for service in services:
        if ignore_service(service, lang):
            continue
        if "methods" in service:
            methods = service["methods"]
            if methods is None:
                raise NotImplementedError("Methods not found for service " + service)

        service_name = service["name"]
        for method in service["methods"]:
            if ignore_method(service, method, lang):
                continue

            method["request"]["id"] = int(id_fmt % (service["id"], method["id"], 0), 16)
            method["response"]["id"] = int(id_fmt % (service["id"], method["id"], 1), 16)
            events = method.get("events", None)
            if events is not None:
                for i in range(len(events)):
                    method["events"][i]["id"] = int(
                        id_fmt % (service["id"], method["id"], i + 2), 16
                    )

            method_name = method["name"]
            codec_file_name = file_name_generators[lang](service_name, method_name)
            contains_serialized_data_in_request = data_containing_requests[service_name][method_name]
            try:
                if lang is SupportedLanguages.CPP:
                    codec_template = env.get_template("codec-template.h.j2")
                    content = codec_template.render(
                        service_name=service_name,
                        method=method,
                        contains_serialized_data_in_request=contains_serialized_data_in_request
                    )
                    save_file(join(output_dir, "codecs.h"), content, "a+")

                    codec_template = env.get_template("codec-template.cpp.j2")
                    content = codec_template.render(
                        service_name=service_name,
                        method=method,
                        contains_serialized_data_in_request=contains_serialized_data_in_request
                    )
                    save_file(join(output_dir, "codecs.cpp"), content, "a+")
                else:
                    content = template.render(
                        service_name=service_name,
                        method=method,
                        contains_serialized_data_in_request=contains_serialized_data_in_request
                    )
                    save_file(join(output_dir, codec_file_name), content)
            except NotImplementedError as e:
                print("[%s] contains missing type mapping so ignoring it. Error: %s" % (codec_file_name, e))

    if lang is SupportedLanguages.CPP:
        f = open(join(cpp_dir, "footer.txt"), "r")
        content = f.read()
        save_file(join(output_dir, "codecs.h"), content, "a+")
        save_file(join(output_dir, "codecs.cpp"), content, "a+")


def generate_custom_codecs(services, template, output_dir, lang, env):
    makedirs(output_dir, exist_ok=True)
    if lang == SupportedLanguages.CPP:
        cpp_header_template = env.get_template("custom-codec-template.h.j2")
        cpp_source_template = env.get_template("custom-codec-template.cpp.j2")
    for service in services:
        if "customTypes" in service:
            custom_types = service["customTypes"]
            for codec in custom_types:
                if ignore_service(codec, lang):
                    continue
                try:
                    if lang == SupportedLanguages.CPP:
                        file_name_prefix = codec["name"].lower() + "_codec"
                        header_file_name = file_name_prefix + ".h"
                        source_file_name = file_name_prefix + ".cpp"
                        codec_file_name = header_file_name
                        content = cpp_header_template.render(codec=codec)
                        save_file(join(output_dir, header_file_name), content)
                        codec_file_name = source_file_name
                        content = cpp_source_template.render(codec=codec)
                        save_file(join(output_dir, source_file_name), content)
                    else:
                        if lang == SupportedLanguages.TS:
                            # Add a getter method to HazelcastJsonValue because it is public and only has toString() API.
                            if codec["name"] == "HazelcastJsonValue":
                                codec["params"][0]["getterMethod"] = "toString()"
                        codec_file_name = file_name_generators[lang](codec["name"])
                        content = template.render(codec=codec)
                        save_file(join(output_dir, codec_file_name), content)
                except NotImplementedError:
                    print("[%s] contains missing type mapping so ignoring it." % codec_file_name)


def generate_documentation(services, custom_definitions, template, output_dir):
    makedirs(output_dir, exist_ok=True)
    content = template.render(
        services=list(filter(lambda s: s["name"] not in internal_services, services)),
        custom_definitions=custom_definitions,
    )
    file_name = join(output_dir, "documentation.md")
    with open(file_name, "w", newline="\n") as file:
        file.writelines(content)


def item_type(lang_name, param_type):
    if param_type.startswith("List_") or param_type.startswith("ListCN_"):
        return lang_name(param_type.split("_", 1)[1])


def key_type(lang_name, param_type):
    return lang_name(param_type.split("_", 2)[1])


def value_type(lang_name, param_type):
    return lang_name(param_type.split("_", 2)[2])


def is_var_sized_list(param_type):
    return param_type.startswith("List_") and param_type not in FixSizedListTypes


def is_var_sized_list_contains_nullable(param_type):
    return param_type.startswith("ListCN_") and param_type not in FixSizedListTypes


def is_var_sized_map(param_type):
    return param_type.startswith("Map_") and param_type not in FixSizedMapTypes


def is_var_sized_entry_list(param_type):
    return param_type.startswith("EntryList_") and param_type not in FixSizedEntryListTypes


def load_services(protocol_def_dir):
    service_list = listdir(protocol_def_dir)
    services = []
    for service_file in service_list:
        file_path = join(protocol_def_dir, service_file)
        if isfile(file_path):
            with open(file_path, "r") as file:
                try:
                    data = yaml.load(file, Loader=yaml.Loader)
                except MarkedYAMLError as err:
                    print(err)
                    exit(-1)
                services.append(data)
    return services


def validate_services(services, schema_path, no_id_check, protocol_versions):
    valid = True
    with open(schema_path, "r") as schema_file:
        schema = json.load(schema_file)
        for i in range(len(services)):
            service = services[i]
            if not validate_against_schema(service, schema):
                return False

            if not no_id_check and service["name"] not in ID_VALIDATOR_IGNORE_SET:
                service_id = service["id"]
                # Validate id ordering of services.
                if i != service_id:
                    print(
                        "Check the service id of the %s. Expected: %s, found: %s."
                        % (service["name"], i, service_id)
                    )
                    valid = False
                # Validate id ordering of definition methods.
                methods = service["methods"]
                for j in range(len(methods)):
                    method = methods[j]
                    method_id = method["id"]
                    if (j + 1) != method_id:
                        print(
                            "Check the method id of %s#%s. Expected: %s, found: %s"
                            % (service["name"], method["name"], (j + 1), method_id)
                        )
                        valid = False
                    request_params = method["request"].get("params", [])
                    method_name = service["name"] + "#" + method["name"]
                    if not is_parameters_ordered_and_semantically_correct(
                        method["since"], method_name + "#request", request_params, protocol_versions
                    ):
                        valid = False
                    response_params = method["response"].get("params", [])
                    if not is_parameters_ordered_and_semantically_correct(
                        method["since"],
                        method_name + "#response",
                        response_params,
                        protocol_versions,
                    ):
                        valid = False
                    events = method.get("events", [])
                    for event in events:
                        event_params = event.get("params", [])
                        if not is_parameters_ordered_and_semantically_correct(
                            event["since"],
                            method_name + "#" + event["name"] + "#event",
                            event_params,
                            protocol_versions,
                        ):
                            valid = False
    return valid


def is_semantically_correct_param(version, protocol_versions):
    is_semantically_correct = True
    if version != protocol_versions[0]:
        # Not 2.0
        if version % MINOR_VERSION_MULTIPLIER == 0:
            # Minor version
            if (version - MINOR_VERSION_MULTIPLIER) not in protocol_versions:
                # since is set to 2.x but 2.(x-1) is not in the protocol definitions
                is_semantically_correct = False
        elif version % PATCH_VERSION_MULTIPLIER == 0:
            # Patch version
            if (version - PATCH_VERSION_MULTIPLIER) not in protocol_versions:
                # since is set to 2.x.y but 2.x.(y-1) is not in the protocol definitions
                is_semantically_correct = False
    return is_semantically_correct


def is_parameters_ordered_and_semantically_correct(since, name, params, protocol_versions):
    is_ordered = True
    is_semantically_correct = True
    version = get_version_as_number(since)

    if not is_semantically_correct_param(version, protocol_versions):
        method_or_event_name = name[: name.rindex("#")]
        print(
            'Check the since value of the "%s"\n'
            'It is set to version "%s" but this protocol version does '
            "not semantically follow other protocol versions!" % (method_or_event_name, since)
        )
        is_semantically_correct = False

    for param in params:
        param_version = get_version_as_number(param["since"])
        if not is_semantically_correct_param(param_version, protocol_versions):
            print(
                'Check the since value of "%s" field of the "%s".\n'
                'It is set version "%s" but this protocol version does '
                "not semantically follow other protocol versions!"
                % (param["name"], name, param["since"])
            )
            is_semantically_correct = False

        if version > param_version:
            print(
                'Check the since value of "%s" field of the "%s".\n'
                "Parameters should be in the increasing order of since values!"
                % (param["name"], name)
            )
            is_ordered = False

        version = param_version
    return is_ordered and is_semantically_correct


def validate_custom_protocol_definitions(definition, schema_path, protocol_versions):
    valid = True
    with open(schema_path, "r") as schema_file:
        schema = json.load(schema_file)
    custom_types = definition[0]
    if not validate_against_schema(custom_types, schema):
        return False
    for custom_type in custom_types["customTypes"]:
        params = custom_type.get("params", [])
        if not is_parameters_ordered_and_semantically_correct(
            custom_type["since"], "CustomTypes#" + custom_type["name"], params, protocol_versions
        ):
            valid = False
    return valid


def validate_against_schema(service, schema):
    try:
        jsonschema.validate(service, schema)
    except jsonschema.ValidationError as e:
        print("Validation error on %s: %s" % (service.get("name", None), e))
        return False
    return True


def save_file(file, content, mode="w"):

    if file.endswith(".cs"):
        content = content.replace("\r\n", "\n") # crlf -> lf
        content = content.replace("\r", "\n")   # cr -> lf
        content = re.sub("[ \t]+$", "", content, 0, re.M)
        content = content.rstrip("\n")          # trim all trailing lf
        content = content  + "\n"             # append one single trailing lf

    m = hashlib.md5()
    m.update(content.encode("utf-8"))
    codec_hash = m.hexdigest()
    with open(file, mode, newline=os.linesep) as file:
        file.writelines(content.replace("!codec_hash!", codec_hash))


def get_protocol_versions(protocol_defs, custom_codec_defs):
    protocol_versions = set()
    if not custom_codec_defs:
        custom_codec_defs = []
    else:
        custom_codec_defs = custom_codec_defs[0]["customTypes"]

    for service in protocol_defs:
        for method in service["methods"]:
            protocol_versions.add(method["since"])
            for req_param in method["request"].get("params", []):
                protocol_versions.add(req_param["since"])
            for res_param in method["response"].get("params", []):
                protocol_versions.add(res_param["since"])
            for event in method.get("events", []):
                protocol_versions.add(event["since"])
                for event_param in event.get("params", []):
                    protocol_versions.add(event_param["since"])

    for custom_codec in custom_codec_defs:
        protocol_versions.add(custom_codec["since"])
        for param in custom_codec.get("params", []):
            protocol_versions.add(param["since"])

    return map(str, protocol_versions)


class SupportedLanguages(Enum):
    JAVA = "java"
    CPP = "cpp"
    CS = "cs"
    PY = "py"
    TS = "ts"
    # GO = 'go'
    MD = "md"


codec_output_directories = {
    SupportedLanguages.JAVA: "hazelcast/src/main/java/com/hazelcast/client/impl/protocol/codec/",
    SupportedLanguages.CPP: "hazelcast/generated-sources/src/hazelcast/client/protocol/codec/",
    SupportedLanguages.CS: "src/Hazelcast.Net/Protocol/Codecs/",
    SupportedLanguages.PY: "hazelcast/protocol/codec/",
    SupportedLanguages.TS: "src/codec/",
    # SupportedLanguages.GO: 'internal/proto/'
    SupportedLanguages.MD: "documentation",
}

custom_codec_output_directories = {
    SupportedLanguages.JAVA: "hazelcast/src/main/java/com/hazelcast/client/impl/protocol/codec/custom/",
    SupportedLanguages.CPP: "hazelcast/generated-sources/src/hazelcast/client/protocol/codec/",
    SupportedLanguages.CS: "src/Hazelcast.Net/Protocol/CustomCodecs/",
    SupportedLanguages.PY: "hazelcast/protocol/codec/custom/",
    SupportedLanguages.TS: "src/codec/custom",
    # SupportedLanguages.GO: 'internal/proto/'
}


def _capitalized_name_generator(extension):
    def inner(*names):
        return "%sCodec.%s" % ("".join(map(capital, names)), extension)

    return inner


def _snake_cased_name_generator(extension):
    def inner(*names):
        return "%s_codec.%s" % ("_".join([py_param_name(name, False) for name in names]), extension)

    return inner


file_name_generators = {
    SupportedLanguages.JAVA: _capitalized_name_generator("java"),
    SupportedLanguages.CPP: _snake_cased_name_generator("cpp"),
    SupportedLanguages.CS: _capitalized_name_generator("cs"),
    SupportedLanguages.PY: _snake_cased_name_generator("py"),
    SupportedLanguages.TS: _capitalized_name_generator("ts"),
    # SupportedLanguages.GO: 'go'
    SupportedLanguages.MD: "md",
}

language_specific_funcs = {
    SupportedLanguages.JAVA: {
        "lang_types_encode": java_types_encode,
        "lang_types_decode": java_types_decode,
        "lang_name": java_name,
        "param_name": param_name,
    },
    SupportedLanguages.CS: {
        "lang_types_encode": cs_types_encode,
        "lang_types_decode": cs_types_decode,
        "lang_name": cs_name,
        "param_name": param_name,
        "escape_keyword": cs_escape_keyword,
        "custom_codec_param_name": cs_custom_codec_param_name,
        "cs_sizeof": cs_sizeof,
    },
    SupportedLanguages.CPP: {
        "lang_types_encode": cpp_types_encode,
        "lang_types_decode": cpp_types_decode,
        "lang_name": cpp_name,
        "param_name": cpp_param_name,
    },
    SupportedLanguages.TS: {
        "lang_types_encode": ts_types_encode,
        "lang_types_decode": ts_types_decode,
        "lang_name": java_name,
        "param_name": param_name,
        "escape_keyword": ts_escape_keyword,
        "get_import_path_holders": ts_get_import_path_holders,
    },
    SupportedLanguages.PY: {
        "lang_types_encode": py_types_encode_decode,
        "lang_types_decode": py_types_encode_decode,
        "lang_name": java_name,
        "param_name": py_param_name,
        "escape_keyword": py_escape_keyword,
        "get_import_path_holders": py_get_import_path_holders,
        "custom_type_name": py_custom_type_name,
        "decoder_requires_to_object_fn": py_decoder_requires_to_object_fn,
        "to_object_fn_in_decode": py_to_object_fn_in_decode,
    }
}

language_service_ignore_list = {
    SupportedLanguages.JAVA: set(),
    SupportedLanguages.CPP: cpp_ignore_service_list,
    SupportedLanguages.CS: cs_ignore_service_list,
    SupportedLanguages.PY: py_ignore_service_list,
    SupportedLanguages.TS: ts_ignore_service_list,
    # SupportedLanguages.GO: set()
}


def ignore_service(service, lang):
    name = service["name"]
    return ignore_service_or_method(name, lang)


def ignore_method(service, method, lang):
    name = service["name"] + "." + method["name"]
    return ignore_service_or_method(name, lang)


def ignore_service_or_method(name, lang):
    patterns = language_service_ignore_list[lang]
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            print("[%s] is in ignore list so ignoring it." % name)
            return True
    return False


def create_environment(lang, namespace):
    env = Environment(
        loader=PackageLoader(lang.value, "."),
        extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"],
    )
    env.trim_blocks = True
    env.lstrip_blocks = True
    env.keep_trailing_newline = False
    env.filters["capital"] = capital
    env.globals["to_upper_snake_case"] = to_upper_snake_case
    env.globals["fixed_params"] = fixed_params
    env.globals["var_size_params"] = var_size_params
    env.globals["new_params"] = new_params
    env.globals["filter_new_params"] = filter_new_params
    env.globals["is_var_sized_list"] = is_var_sized_list
    env.globals["is_var_sized_list_contains_nullable"] = is_var_sized_list_contains_nullable
    env.globals["is_var_sized_entry_list"] = is_var_sized_entry_list
    env.globals["is_var_sized_map"] = is_var_sized_map
    env.globals["item_type"] = item_type
    env.globals["key_type"] = key_type
    env.globals["value_type"] = value_type
    env.globals["namespace"] = namespace
    env.globals["get_size"] = get_size
    env.globals["is_trivial"] = is_trivial
    env.globals["copyright_year"] = date.today().year
    
    try:
        with os.popen("git rev-parse --short HEAD") as f:
            env.globals["protocol_commit"] = f.readlines()[0].strip()
    except:
        env.globals["protocol_commit"] = "unknown"

    for fn_name, fn in language_specific_funcs[lang].items():
        env.globals[fn_name] = fn

    return env
