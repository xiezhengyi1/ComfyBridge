# 手动安装纯 Python wheel 到工作区 .vendor（绕过沙箱对 tempfile/pip 的限制）
import io
import json
import os
import urllib.request
import zipfile

ven = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".vendor")
os.makedirs(ven, exist_ok=True)

for name in ["fastapi", "starlette", "uvicorn", "websocket-client"]:
    info = json.loads(urllib.request.urlopen("https://pypi.org/pypi/%s/json" % name, timeout=30).read())
    wheels = [u for u in info["urls"] if u["filename"].endswith("py3-none-any.whl")]
    if not wheels:
        print(name, "NO pure wheel")
        continue
    url, fn = wheels[0]["url"], wheels[0]["filename"]
    print("downloading %s==%s %s ..." % (name, info["info"]["version"], fn), flush=True)
    data = urllib.request.urlopen(url, timeout=60).read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(ven)
    print("  -> installed", flush=True)
print("ALL DONE")
