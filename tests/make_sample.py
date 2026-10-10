"""合成データ（偽ログ）からサンプルレポートを sample/ に生成する。実ログは使わない。"""
import os, sys, tempfile, shutil
HERE = os.path.dirname(os.path.abspath(__file__)); PROTO = os.path.dirname(HERE); SCRIPTS = os.path.join(PROTO, 'skills', 'tokdiet', 'scripts')
sys.path.insert(0, SCRIPTS); sys.path.insert(0, HERE)
import tokdiet, synth
tmp = tempfile.mkdtemp()
root = synth.build(os.path.join(tmp, "projects"))
md, html, _ = tokdiet.run(root, tmp, brief=True)
for src, name in ((md, "synthetic-report.md"), (html, "synthetic-report.html")):
    shutil.copy(src, os.path.join(PROTO, "sample", name))
shutil.copy(os.path.join(tmp, "action-brief.md"), os.path.join(PROTO, "sample", "synthetic-action-brief.md"))
print("sample/ に合成レポートを生成しました")
