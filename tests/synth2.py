"""M1.2 用の合成環境: 2セッション（古い一覧→新しい一覧）、プロジェクトの .claude 一式、サードパーティ配下、成果物/参照資料。"""
import json, os

J = lambda x: json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def rec(t, **kw):
    d = {"type": t, "timestamp": kw.pop("ts"), "cwd": kw.pop("cwd")}
    d.update(kw)
    return d


def asst(rid, ts, cwd, blocks, out=10):
    return rec("assistant", ts=ts, cwd=cwd, requestId=rid, isSidechain=False,
               message={"id": "m" + rid, "model": "claude-sonnet-5", "role": "assistant", "content": blocks,
                        "usage": {"input_tokens": 1, "output_tokens": out, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10}})


def tu(i, name, **inp):
    return {"type": "tool_use", "id": "toolu_" + i, "name": name, "input": inp}


def res(i, n, ts, cwd):
    return rec("user", ts=ts, cwd=cwd, message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_" + i, "content": "x" * n}]})


def listing(ts, cwd, names, count):
    content = "".join("- %s: desc of %s\n" % (n, n) for n in names)
    return rec("attachment", ts=ts, cwd=cwd, attachment={"type": "skill_listing", "content": content, "skillCount": count, "names": names})


def build(base):
    repo = os.path.join(base, "repo")
    for d in (".git", ".claude/commands", ".claude/agents", ".claude/skills/shared", "themes/x", "docs"):
        os.makedirs(os.path.join(repo, d), exist_ok=True)
    open(os.path.join(repo, ".claude/commands/new-article.md"), "w").write("---\ndescription: write\n---\n執筆。`/wp-media` を呼び、writer エージェントに渡す。\n")
    open(os.path.join(repo, ".claude/agents/writer.md"), "w").write("---\nname: writer\n---\nbody\n")
    open(os.path.join(repo, ".claude/skills/shared/SKILL.md"), "w").write("---\nname: shared\ndescription: d\n---\n")
    open(os.path.join(repo, "themes/x/README.md"), "w").write("old-skill is mentioned in a third party theme: old-skill\n")
    open(os.path.join(repo, "CLAUDE.md"), "w").write("# rules\nGoogle Drive のコネクタで台帳を読む。\n")
    open(os.path.join(repo, "docs/out.md"), "w").write("deliverable\n")
    open(os.path.join(repo, "docs/ref.md"), "w").write("reference\n")
    out_path, ref_path = os.path.join(repo, "docs/out.md"), os.path.join(repo, "docs/ref.md")
    root = os.path.join(base, "projects"); d = os.path.join(root, "-repo")
    os.makedirs(os.path.join(d, "sess-A", "subagents"))
    names_old = ["new-article", "wp-media", "wp:media", "old-skill", "shared"]
    A = [
        listing("2026-01-01T00:00:00Z", repo, names_old, 5),
        asst("a1", "2026-01-01T00:01:00Z", repo, [tu("s1", "Skill", skill="shared"), tu("w1", "Write", file_path=out_path),
                                                    tu("r1", "Read", file_path=out_path), tu("r2", "Read", file_path=out_path), tu("r3", "Read", file_path=out_path)]),
        asst("a2", "2026-01-01T00:02:00Z", repo, [tu("f%d" % i, "Read", file_path=ref_path) for i in range(4)]),
    ]
    open(os.path.join(d, "sess-A.jsonl"), "w").write("\n".join(J(x) for x in A) + "\n")
    sub = [listing("2026-01-01T00:00:30Z", repo, ["sub-only-skill"], 99), asst("sa1", "2026-01-01T00:00:40Z", repo, [])]
    for x in sub:
        x["isSidechain"] = True
    open(os.path.join(d, "sess-A", "subagents", "agent-1.jsonl"), "w").write("\n".join(J(x) for x in sub) + "\n")
    open(os.path.join(d, "sess-A", "subagents", "agent-1.meta.json"), "w").write(J({"agentType": "writer"}))
    B = [
        listing("2026-01-02T00:00:00Z", repo, ["new-article", "wp-media", "wp:media", "shared"], 4),
        rec("attachment", ts="2026-01-02T00:00:01Z", cwd=repo, attachment={"type": "mcp_instructions_delta", "addedNames": ["claude_ai_Google_Drive", "other_srv"], "addedBlocks": ["x" * 50, "y" * 50]}),
        asst("b1", "2026-01-02T00:01:00Z", repo, []),
    ]
    open(os.path.join(d, "sess-B.jsonl"), "w").write("\n".join(J(x) for x in B) + "\n")
    return root, repo, out_path, ref_path
