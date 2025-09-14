import os, zipfile

def zip_dir(src_dir: str, zip_path: str) -> None:
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for folder, _, files in os.walk(src_dir):
            for fn in files:
                full = os.path.join(folder, fn)
                arc = os.path.relpath(full, src_dir)
                z.write(full, arcname=arc)                
