"""合成jsonl（偽のログ）を作る。実ログは一切使わない。"""
import json, os

SECRET_PATH = "/home/secretuser/repos/acme-secretrepo/src/secret_module_name.py"
SECRET_PROMPT = "SECRET_PROMPT_TEXT please summarize the quarterly numbers for acme"
SECRET_KEY = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX"
SECRET_EMAIL = "boss@acme-corp.example"
SECRET_GH = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
INSTR_PATH = "/home/secretuser/repos/acme-secretrepo/CLAUDE.md"
INSTR_BODY = "# Title\nintro\n## Big Section\n" + "x" * 3000 + "\n## Small\nshort\n"
BASH_CMD = "curl -H 'Authorization: Bearer %s' https://api.example.com/v1/items?token=%s && echo %s" % (SECRET_KEY, SECRET_GH, SECRET_EMAIL)
SKILL_LISTING = "- big-skill: " + "d" * 500 + "\n- plug:colon-skill: 12345\n- unused-skill: short desc\n- used-skill: tiny\n"
GUIDE_PATH = "/home/secretuser/repos/acme-secretrepo/docs/guide.md"
RANGED_PATH = "/home/secretuser/repos/acme-secretrepo/docs/ranged.md"
FULL_PATH = "/home/secretuser/repos/acme-secretrepo/docs/full.md"
SCRIPT_CMD = "bash ops/some-long-script-name.sh --verbose"
SECRET_DIR = "-home-secretuser-repos-acme-secretrepo"


def A(rid, ts, model="claude-sonnet-5", inp=100, out=50, cr=1000, cw=500, blocks=None, side=False, **extra):
    d = {"type": "assistant", "requestId": rid, "timestamp": ts, "isSidechain": side,
         "cwd": "/home/secretuser/repos/acme-secretrepo",
         "message": {"id": "m_" + rid, "model": model, "role": "assistant", "content": blocks or [],
                     "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                               "cache_creation_input_tokens": cw}}}
    d.update(extra)
    return d


def tool_use(i, name, **inp):
    return {"type": "tool_use", "id": "toolu_" + i, "name": name, "input": inp}


def result(i, n):
    return {"type": "user", "timestamp": "2026-01-01T00:00:05Z", "cwd": "/home/secretuser/repos/acme-secretrepo",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_" + i, "content": "x" * n}]}}


def build(root):
    d = os.path.join(root, SECRET_DIR)
    os.makedirs(os.path.join(d, "sess-0001", "subagents"))
    main = [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": "<command-name>/deploy-all</command-name>"}},
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": SECRET_PROMPT}},
        {"type": "attachment", "timestamp": "2026-01-01T00:00:00Z", "attachment": {"type": "instructions",
         "files": [{"path": INSTR_PATH, "type": "Project", "content": INSTR_BODY}]}},
        {"type": "attachment", "timestamp": "2026-01-01T00:00:00Z", "attachment": {"type": "skill_listing",
         "content": SKILL_LISTING, "skillCount": 4, "names": ["big-skill", "plug:colon-skill", "unused-skill", "used-skill"]}},
        # 同一requestIdが2行（ブロック分割）: outputは後の行が大きい
        A("r1", "2026-01-01T00:00:01Z", out=30, blocks=[{"type": "thinking", "thinking": "x"}]),
        A("r1", "2026-01-01T00:00:02Z", out=80, blocks=[tool_use("t1", "Read", file_path=SECRET_PATH)]),
        result("t1", 50),
        A("r2", "2026-01-01T01:00:00Z", inp=10, out=20, cr=2000, cw=0, blocks=[tool_use("t2", "Read", file_path=SECRET_PATH)]),
        result("t2", 50),
        A("r2b", "2026-01-01T01:10:00Z", blocks=[tool_use("t3", "Bash", command=BASH_CMD), tool_use("t4", "Skill", skill="used-skill")],
          wireToolInputs={"toolu_t4": {"skill": "used-skill"}}),
        A("r2c", "2026-01-01T01:11:00Z", blocks=[tool_use("t5", "Bash", command=BASH_CMD)]),
        A("r2d", "2026-01-01T01:12:00Z", blocks=[tool_use("t6", "Bash", command=BASH_CMD)] +
          [tool_use("ts%d" % i, "Bash", command=SCRIPT_CMD) for i in range(3)]),
        A("rl", "2026-01-01T02:00:00Z", model="<synthetic>", error="rate_limit", isApiErrorMessage=True,
          blocks=[{"type": "text", "text": "You've hit your session limit"}]),
    ]
    with open(os.path.join(d, "sess-0001.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(x, separators=(",", ":")) for x in main) + "\n")
    sub = [A("r3", "2026-01-01T00:30:00Z", inp=5, out=7, cr=100, cw=0, side=True,
             blocks=[tool_use("g1", "Read", file_path=GUIDE_PATH), tool_use("g2", "Read", file_path=GUIDE_PATH)] +
             [tool_use("f%d" % i, "Read", file_path=FULL_PATH) for i in range(4)])]
    with open(os.path.join(d, "sess-0001", "subagents", "agent-abc.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(x, separators=(",", ":")) for x in sub) + "\n")
    with open(os.path.join(d, "sess-0001", "subagents", "agent-abc.meta.json"), "w") as f:
        json.dump({"agentType": "my-agent", "description": "d"}, f)
    sub2 = [A("r4", "2026-01-01T00:40:00Z", inp=1, out=1, cr=10, cw=0, side=True,
              blocks=[tool_use("q%d" % i, "Read", file_path=RANGED_PATH, offset=1, limit=50) for i in range(4)])]
    with open(os.path.join(d, "sess-0001", "subagents", "agent-def.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(x, separators=(",", ":")) for x in sub2) + "\n")
    with open(os.path.join(d, "sess-0001", "subagents", "agent-def.meta.json"), "w") as f:
        json.dump({"agentType": "ranged-agent", "description": "d"}, f)
    return root
