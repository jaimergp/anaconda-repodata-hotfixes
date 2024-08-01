import requests
import logging
import json
import re
from collections import defaultdict
from pathlib import Path
from conda.models.version import VersionOrder

numpy2_protect_dict = {
    'pandas': '2.2.2',
    'scikit-learn': '1.4.2',
    'pyamg': '4.2.3',
    'pyqtgraph': '0.13.1'
}

proposed_changes = []

# Configure the logging
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Create a logger object
logger = logging.getLogger(__name__)

CHANNEL_NAME = "main"
CHANNEL_ALIAS = "https://repo.anaconda.com/pkgs"
SUBDIRS = (
    "noarch",
    "linux-64",
    "linux-aarch64",
    "linux-s390x",
    "osx-64",
    "osx-arm64",
    "win-64",
)


# Initialize NUMPY_2_CHANGES with a nested defaultdict structure
NUMPY_2_CHANGES = defaultdict(lambda: defaultdict(dict))


def collect_proposed_change(subdirectory, filename, change_type, original_dependency, updated_dependency, reason):
    """
    Collects a proposed change to a dependency for later processing.

    Parameters:
    - subdirectory: The subdirectory where the file is located.
    - filename: The name of the file being modified.
    - change_type: The type of change (e.g., 'dep', 'constr').
    - original_dependency: The original dependency string.
    - updated_dependency: The updated dependency string.
    - reason: The reason for the change.
    """
    # change dep and constr to dependency and constraint
    if change_type == "dep":
        change_type = "depends"
    elif change_type == "constr":
        change_type = "constrains"

    NUMPY_2_CHANGES[subdirectory][filename] = {
        "type": change_type,
        "original": original_dependency,
        "updated": updated_dependency
    }

    logger.info(f"numpy 2.0.0: {reason} for {filename}. "
                f"Original: '{original_dependency}' -> New: '{updated_dependency}' ({reason})")


def parse_version(version_str):
    """
    Extracts the version number from a version string.

    Parameters:
    - version_str: The version string to parse.

    Returns:
    The extracted version number or None if not found.
    """
    match = re.search(r'(\d+(\.\d+)*)', version_str)
    return match.group(1) if match else None


def has_upper_bound(dependency):
    """
    Checks if a dependency string contains an upper bound.

    Parameters:
    - dependency: The dependency string to check.

    Returns:
    True if an upper bound is found, False otherwise.
    """
    return any(part.strip().startswith('<') for part in dependency.split(','))


def patch_record_with_fixed_deps(dependency, parts):
    """
    Adds an upper bound to a dependency if necessary.

    Parameters:
    - dependency: The original dependency string.
    - parts: The parts of the dependency string, split by spaces.

    Returns:
    The potentially modified dependency string.
    """
    version_str = parts[1]
    version = parse_version(version_str)
    if version:
        if version_str.startswith('==') or version_str.startswith('<') or version_str[0].isdigit():
            return dependency
        if version_str.startswith('>') or version_str.startswith('>='):
            return f"{dependency},<2.0a0"
        return f"{dependency} <2.0a0"
    return dependency


def update_numpy_dependencies(dependencies_list, package_record, dependency_type, package_subdir, filename):
    """
    Adds upper bounds to numpy dependencies as needed.
    Iterates through dependencies, modifying those without upper bounds and meeting specific criteria.

    Parameters:
    - dependencies_list: Dependencies to check and modify.
    - package_record: Metadata about the current package.
    - dependency_type: Type of dependency ('run', 'build').
    - package_subdir: Package location subdirectory.
    - filename: Package filename.
    """
    add_bound_to_unspecified = True
    for _, dependency in enumerate(dependencies_list):
        parts = dependency.split()
        package_name = parts[0]
        if package_name in ["numpy", "numpy-base"]:
            if not has_upper_bound(dependency):
                if package_name in numpy2_protect_dict:
                    version_str = parts[1] if len(parts) > 1 else None
                    version = parse_version(version_str) if version_str else None
                    protected_version = parse_version(numpy2_protect_dict[package_name])
                    if version and protected_version:
                        try:
                            if VersionOrder(version) <= VersionOrder(protected_version):
                                new_dependency = f"{dependency},<2.0a0" if len(parts) > 1 else f"{dependency} <2.0a0"
                                collect_proposed_change(package_subdir, filename, dependency_type, dependency,
                                                        new_dependency, "Version <= protected_version")
                        except ValueError:
                            new_dependency = f"{dependency},<2.0a0" if len(parts) > 1 else f"{dependency} <2.0a0"
                            collect_proposed_change(package_subdir, filename, dependency_type, dependency,
                                                    new_dependency, "Version comparison failed")
                elif add_bound_to_unspecified:
                    if len(parts) > 1:
                        new_dependency = patch_record_with_fixed_deps(dependency, parts)
                        if new_dependency != dependency:
                            collect_proposed_change(package_subdir, filename, dependency_type, dependency,
                                                    new_dependency, "Upper bound added")
                    else:
                        new_dependency = f"{dependency} <2.0a0"
                        collect_proposed_change(package_subdir, filename, dependency_type, dependency,
                                                new_dependency, "Upper bound added")


def main():
    base_dir = Path(__file__).parent / CHANNEL_NAME
    repodatas = {}
    for subdir in SUBDIRS:
        repodata_path = base_dir / subdir / "repodata_from_packages.json"
        if repodata_path.is_file():
            with repodata_path.open() as fh:
                repodatas[subdir] = json.load(fh)
        else:
            repodata_url = f"{CHANNEL_ALIAS}/{CHANNEL_NAME}/{subdir}/repodata_from_packages.json"
            response = requests.get(repodata_url)
            response.raise_for_status()
            repodatas[subdir] = response.json()
            repodata_path.parent.mkdir(parents=True, exist_ok=True)
            with repodata_path.open('w') as fh:
                json.dump(
                    repodatas[subdir],
                    fh,
                    indent=2,
                    sort_keys=True,
                    separators=(",", ": "),
                )
    for subdir in SUBDIRS:
        index = repodatas[subdir]["packages"]
        for fn, record in index.items():
            name = record["name"]
            depends = record["depends"]
            constrains = record.get("constrains", [])

            depends = [dep for dep in depends if dep is not None]
            if any(py_ver in fn for py_ver in ["py39", "py310", "py311", "py312"]):
                if name not in ["anaconda", "_anaconda_depends", "__anaconda_core_depends", "_anaconda_core"]:
                    try:
                        for dep in depends:
                            if dep.split()[0] in ["numpy", "numpy-base"]:
                                update_numpy_dependencies(depends, record, "dep", subdir, fn)
                        for constrain in constrains:
                            if constrain.split()[0] in ["numpy", "numpy-base"]:
                                update_numpy_dependencies(constrains, record, "constr", subdir, fn)
                    except Exception as e:
                        logger.error(f"numpy 2.0.0 error {fn}: {e}")

    json_filename = Path("numpy2_patch.json")
    with json_filename.open('w') as f:
        json.dump(dict(NUMPY_2_CHANGES), f, indent=2)

    logger.info(f"Proposed changes have been written to {json_filename}")


if __name__ == "__main__":
    main()
