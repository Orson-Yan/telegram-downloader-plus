"""
Handle duplicate files.

A file is considered a duplicate if it has the same md5 checksum as another
file with the same base name.
"""
import glob
import hashlib
import os
import pathlib


def handle_dup_file(file_path: str) -> str:
    """
    Handle duplicate files.

    Parameters
    ----------
    file_path: str
        Path to file.

    Returns
    -------
    str
        Returns the path to the duplicate file if one is found; otherwise
        returns the path to the original file.
    """
    posix_path = pathlib.Path(file_path)
    file_base_name: str = "".join(posix_path.stem.split("-copy")[0])
    name_pattern: str = f"{posix_path.parent}/{file_base_name}*"
    # Reason for using `str.translate()`
    # https://stackoverflow.com/q/22055500/6730439
    old_files: list = glob.glob(
        name_pattern.translate({ord("["): "[[]", ord("]"): "[]]"})
    )
    if file_path in old_files:
        old_files.remove(file_path)
    with open(file_path, "rb") as f:
        current_file_md5: str = hashlib.md5(f.read()).hexdigest()
    for old_file_path in old_files:
        with open(old_file_path, "rb") as f:
            old_file_md5: str = hashlib.md5(f.read()).hexdigest()
        if current_file_md5 == old_file_md5:
            os.remove(file_path)
            return old_file_path
    return file_path