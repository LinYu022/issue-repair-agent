import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const labels = {
  viewer: "报告读者",
  planner: "执行者",
  approver: "审批者",
  operator: "恢复操作员",
  curator: "记忆管理员",
};
const json = (value) => JSON.stringify(value, null, 2);
function Detail({ title, value, open = false }) {
  return (
    <details open={open}>
      <summary>{title}</summary>
      <pre>{typeof value === "string" ? value : json(value)}</pre>
    </details>
  );
}
function App() {
  const [token, setToken] = useState(""),
    [role, setRole] = useState(""),
    [tasks, setTasks] = useState([]),
    [key, setKey] = useState("");
  const [task, setTask] = useState(null),
    [tab, setTab] = useState("issue"),
    [report, setReport] = useState(null),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("本机操作者确认"),
    [source, setSource] = useState(""),
    [target, setTarget] = useState({
      skill_id: "",
      revision: 1,
      document_hash: "",
    }),
    [expected, setExpected] = useState({ revision: "", document_hash: "" }),
    [review, setReview] = useState(null),
    [memoryRecord, setMemoryRecord] = useState(null),
    [candidate, setCandidate] = useState("");
  async function api(path, method = "GET", body) {
    const res = await fetch(path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      ...(body ? { body: json(body) } : {}),
    });
    const value = await res.json();
    if (!res.ok)
      throw new Error(
        `${res.status} · ${typeof value.detail === "string" ? value.detail : json(value.detail)}`,
      );
    return value;
  }
  async function run(fn) {
    setBusy(true);
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setNotice(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function refresh(selected = key) {
    setTasks(await api("/api/tasks"));
    if (selected) setTask(await api(`/api/tasks/${selected}`));
  }
  async function connect(e) {
    e.preventDefault();
    run(async () => {
      const s = await api("/api/session");
      setRole(s.role);
      await refresh();
      setNotice(`已连接：${labels[s.role]}`);
    });
  }
  useEffect(() => {
    setReport(null);
    setReview(null);
    if (key && role) run(async () => setTask(await api(`/api/tasks/${key}`)));
  }, [key]);
  const planning = task?.planning,
    plan = planning?.plan,
    state = planning?.task;
  async function action(path, body, method = "POST") {
    await api(path, method, body);
    await refresh();
    setNotice("操作已记录，状态已刷新。");
  }
  async function recoverDecision() {
    await action("/api/recovery/decision", {
      recovery_id: review.request.recovery_id,
      review_hash: review.review_hash,
      plan_hash: review.plan_hash,
      decision: "approve",
      expires_at: new Date(Date.now() + 180000).toISOString(),
      reason,
    });
  }
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <span className="mark">↗</span> ISSUE REPAIR
        </div>
        <p className="muted">证据驱动的修复工作台</p>
        <form onSubmit={connect}>
          <label htmlFor="credential">本机角色凭证</label>
          <input
            id="credential"
            type="password"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              setRole("");
              setTask(null);
              setTasks([]);
              setReport(null);
            }}
            autoComplete="off"
          />
          <button disabled={busy}>连接工作台</button>
        </form>
        <div className="role">{labels[role] || "尚未连接"}</div>
        <h2>
          任务档案 <small>{tasks.length}</small>
        </h2>
        <nav aria-label="任务列表">
          {tasks.map((t) => (
            <button
              className={key === t.id ? "selected task" : "task"}
              key={t.id}
              onClick={() => {
                setKey(t.id);
                setTab("issue");
              }}
            >
              <b>{t.issue.repo}</b>
              <span>{t.id}</span>
              <small>
                {t.evidence_kind === "real_model_history"
                  ? "真实模型 · 历史只读"
                  : "脚本演示 · 真实执行"}
              </small>
            </button>
          ))}
        </nav>
      </aside>
      <main>
        <header>
          <div>
            <p className="eyebrow">P2-18 / EXECUTION & EVIDENCE</p>
            <h1>让每一次修复都有据可查</h1>
          </div>
          <button
            className="secondary"
            disabled={!role || busy}
            onClick={() => run(() => refresh())}
          >
            刷新状态
          </button>
        </header>
        {notice && (
          <div role="status" className="notice">
            {notice}
          </div>
        )}
        {!task ? (
          <section className="empty">
            <h2>从左侧选择一个任务</h2>
            <p>先连接本机角色凭证，再查看问题、修复轨迹与关闭后的独立报告。</p>
            <p>角色凭证仅保存在当前页面内存；刷新页面后重新连接。</p>
          </section>
        ) : (
          <>
            <div className="meta">
              <span>{task.read_only ? "历史档案 · 只读" : "实时任务"}</span>
              <span>{task.evidence_kind}</span>
              <code>{task.issue.repo}</code>
            </div>
            <div className="stats">
              <article>
                <small>执行状态</small>
                <strong>{state?.lifecycle || "未知"}</strong>
              </article>
              <article>
                <small>公开自测状态</small>
                <strong>{state?.self_test_status || "NOT_RUN"}</strong>
                <small>仅代表有限公开检查</small>
              </article>
              <article>
                <small>公开闭环完成</small>
                <strong>{task.public_complete ? "已完成" : "未完成"}</strong>
              </article>
              <article>
                <small>独立解决状态</small>
                <strong>{report?.evaluation || "见独立报告"}</strong>
                <small>自测通过不等于独立解决</small>
              </article>
            </div>
            <div className="version">
              <span>代码版本</span>
              <code>{state?.workspace_version || "未记录"}</code>
              <span>基线提交</span>
              <code>{task.issue.base_commit || "教学夹具"}</code>
            </div>
            <div className="tabs" role="tablist">
              {[
                ["issue", "问题与计划"],
                ["trace", "调用与证据"],
                ["diff", "补丁"],
                ["checks", "公开自测"],
                ["recovery", "恢复"],
                ["memory", "Memory / Skill"],
                ["report", "独立报告"],
              ].map(([v, n]) => (
                <button
                  role="tab"
                  aria-selected={tab === v}
                  className={tab === v ? "active" : ""}
                  key={v}
                  onClick={() => setTab(v)}
                >
                  {n}
                </button>
              ))}
            </div>
            <section className="panel">
              {tab === "issue" && (
                <>
                  <h2>Issue</h2>
                  <pre className="prose">{task.issue.issue}</pre>
                  <Detail title="当前计划与范围" value={plan} open />
                  <Detail
                    title="授权与版本绑定"
                    value={{
                      authorization: planning?.authorization,
                      active: planning?.authorization_active,
                      revision_hash: planning?.revision_hash,
                    }}
                  />
                  <Detail title="活跃假设与反证" value={task.hypothesis} />
                  {!task.read_only && (
                    <div className="actions">
                      <label>
                        操作理由
                        <input
                          value={reason}
                          onChange={(e) => setReason(e.target.value)}
                        />
                      </label>
                      <button
                        disabled={busy || role !== "planner"}
                        onClick={() =>
                          run(() => action(`/api/demos/${key}/advance`, {}))
                        }
                      >
                        推进脚本演示
                      </button>
                      <button
                        disabled={busy || role !== "approver" || !plan}
                        onClick={() =>
                          run(() =>
                            action(`/api/tasks/${key}/approval`, {
                              revision_id: plan.revision_id,
                              revision_hash: planning.revision_hash,
                              expected_workspace_version:
                                state.workspace_version,
                              decision: "approve",
                              expires_at: new Date(
                                Date.now() + 180000,
                              ).toISOString(),
                              reason,
                            }),
                          )
                        }
                      >
                        批准当前计划
                      </button>
                      <button
                        className="danger"
                        disabled={busy || role !== "planner"}
                        onClick={() =>
                          run(() =>
                            action(`/api/tasks/${key}/cancel`, {
                              request_id: crypto.randomUUID(),
                              action: "cancel",
                              expected_revision_id: plan?.revision_id || null,
                              reason,
                            }),
                          )
                        }
                      >
                        取消执行
                      </button>
                    </div>
                  )}
                </>
              )}
              {tab === "trace" && (
                <>
                  <h2>
                    调用时间线 <small>{task.calls?.length || 0} 条</small>
                  </h2>
                  <p className="muted">
                    保留状态、版本和来源；UNKNOWN 表示结果未知。
                  </p>
                  {task.calls?.map((c) => (
                    <details key={c.call_id}>
                      <summary>
                        <span className="dot" />
                        {c.operation}{" "}
                        <b className={c.status === "SUCCEEDED" ? "good" : ""}>
                          {c.status}
                        </b>{" "}
                        <small>
                          {c.evidence_kind} · {c.recorded_at}
                        </small>
                      </summary>
                      <pre>{json(c)}</pre>
                      {task.tool_evidence?.[c.call_id] && (
                        <pre>{json(task.tool_evidence[c.call_id])}</pre>
                      )}
                    </details>
                  ))}
                  <Detail title="循环事件" value={task.events} />
                  <Detail title="工件来源与哈希" value={task.provenance} />
                </>
              )}
              {tab === "diff" && (
                <>
                  <h2>提交补丁</h2>
                  <pre className="diff">
                    {(task.diff || "尚无补丁").split("\n").map((l, i) => (
                      <span
                        key={i}
                        className={
                          l.startsWith("+")
                            ? "added"
                            : l.startsWith("-")
                              ? "removed"
                              : ""
                        }
                      >
                        {l}
                        {"\n"}
                      </span>
                    ))}
                  </pre>
                </>
              )}
              {tab === "checks" && (
                <>
                  <h2>公开自测证据</h2>
                  <p>
                    检查结果必须对应当前代码版本。历史通过不能代替修改后的验证。
                  </p>
                  <Detail title="登记检查及结果" value={task.tests} open />
                </>
              )}
              {tab === "recovery" && (
                <>
                  <h2>恢复记录与操作</h2>
                  <Detail title="当前恢复记录" value={task.recovery} open />
                  {task.read_only ? (
                    <p>
                      已关闭的历史任务不能恢复。实时任务须由宿主登记恢复服务。
                    </p>
                  ) : (
                    <div className="actions vertical">
                      <button
                        disabled={busy || role !== "operator"}
                        onClick={() =>
                          run(async () => {
                            setReview(
                              await api("/api/recovery/inspect", "POST", {
                                recovery_id: crypto.randomUUID(),
                                task_id: state.task_id,
                                rebuild_missing_workspace: false,
                              }),
                            );
                            setNotice(
                              "恢复核对已完成；仍运行的进程必须先处理。",
                            );
                          })
                        }
                      >
                        检查恢复条件
                      </button>
                      {["wait", "stop"].map((a) => (
                        <button
                          key={a}
                          disabled={busy || role !== "operator"}
                          onClick={() =>
                            run(() =>
                              action(`/api/recovery/${state.task_id}/quiesce`, {
                                action: a,
                              }),
                            )
                          }
                        >
                          {a === "wait" ? "等待现有进程" : "停止已登记进程"}
                        </button>
                      ))}
                      <Detail title="精确恢复审查" value={review} />
                      <button
                        disabled={
                          busy ||
                          role !== "approver" ||
                          review?.status !== "READY"
                        }
                        onClick={() => run(recoverDecision)}
                      >
                        批准精确恢复计划
                      </button>
                      <button
                        disabled={busy || role !== "operator" || !review}
                        onClick={() =>
                          run(() =>
                            action("/api/recovery/apply", {
                              recovery_id: review.request.recovery_id,
                            }),
                          )
                        }
                      >
                        执行已批准恢复
                      </button>
                    </div>
                  )}
                </>
              )}
              {tab === "memory" && (
                <>
                  <h2>Memory 来源与 Skill 版本</h2>
                  <Detail title="记忆启用状态和来源" value={task.memory} open />
                  <p>
                    记忆不授予执行权限。已撤销来源不能因恢复或回滚重新生效。
                  </p>
                  <div className="actions vertical">
                    <label>
                      来源 UUID
                      <input
                        value={source}
                        onChange={(e) => setSource(e.target.value)}
                      />
                    </label>
                    <button
                      disabled={
                        busy || !["viewer", "curator"].includes(role) || !source
                      }
                      onClick={() =>
                        run(async () =>
                          setMemoryRecord(
                            await api(`/memory/sources/${source}`),
                          ),
                        )
                      }
                    >
                      查看来源状态
                    </button>
                    <Detail title="来源或候选当前记录" value={memoryRecord} />
                    <label>
                      候选记忆 UUID
                      <input
                        value={candidate}
                        onChange={(e) => setCandidate(e.target.value)}
                      />
                    </label>
                    <button
                      disabled={
                        busy ||
                        !["viewer", "curator"].includes(role) ||
                        !candidate
                      }
                      onClick={() =>
                        run(async () =>
                          setMemoryRecord(
                            await api(`/memory/memories/${candidate}`),
                          ),
                        )
                      }
                    >
                      查看记忆候选
                    </button>
                    <button
                      disabled={
                        busy || role !== "curator" || !memoryRecord?.memory_id
                      }
                      onClick={() =>
                        run(() =>
                          action("/memory/memories/review", {
                            request_id: crypto.randomUUID(),
                            memory_id: memoryRecord.memory_id,
                            expected_revision: memoryRecord.revision,
                            expected_hash: memoryRecord.document_hash,
                            action: "promote",
                            reason,
                          }),
                        )
                      }
                    >
                      审查提升当前候选
                    </button>
                    <label>
                      理由
                      <input
                        value={reason}
                        onChange={(e) => setReason(e.target.value)}
                      />
                    </label>
                    <button
                      disabled={busy || role !== "curator" || !source}
                      onClick={() =>
                        run(() =>
                          action(
                            `/api/memory/sources/${source}`,
                            { request_id: crypto.randomUUID(), reason },
                            "DELETE",
                          ),
                        )
                      }
                    >
                      撤销记忆来源
                    </button>
                    <label>
                      Skill 编号
                      <input
                        value={target.skill_id}
                        onChange={(e) =>
                          setTarget({ ...target, skill_id: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      目标版本
                      <input
                        type="number"
                        min="1"
                        value={target.revision}
                        onChange={(e) =>
                          setTarget({
                            ...target,
                            revision: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                    <label>
                      目标版本的文档指纹
                      <input
                        value={target.document_hash}
                        onChange={(e) =>
                          setTarget({
                            ...target,
                            document_hash: e.target.value,
                          })
                        }
                      />
                    </label>
                    <label>
                      当前启用版本（未启用则留空）
                      <input
                        type="number"
                        min="1"
                        value={expected.revision}
                        onChange={(e) =>
                          setExpected({ ...expected, revision: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      当前启用版本的文档指纹
                      <input
                        value={expected.document_hash}
                        onChange={(e) =>
                          setExpected({
                            ...expected,
                            document_hash: e.target.value,
                          })
                        }
                      />
                    </label>
                    <button
                      disabled={
                        busy ||
                        role !== "curator" ||
                        !target.skill_id ||
                        !target.document_hash
                      }
                      onClick={() =>
                        run(() =>
                          action("/api/skills/decision", {
                            request_id: crypto.randomUUID(),
                            target,
                            expected_enabled: expected.revision
                              ? {
                                  skill_id: target.skill_id,
                                  revision: Number(expected.revision),
                                  document_hash: expected.document_hash,
                                }
                              : null,
                            action: "rollback",
                            reason,
                          }),
                        )
                      }
                    >
                      回滚到已验证版本
                    </button>
                  </div>
                </>
              )}
              {tab === "report" && (
                <>
                  <h2>关闭后的独立报告</h2>
                  <p>
                    此处只呈现独立结论与身份哈希，不向执行器提供隐藏测试或评分反馈。
                  </p>
                  <button
                    disabled={busy || role !== "viewer"}
                    onClick={() =>
                      run(async () =>
                        setReport(await api(`/api/reports/${key}`)),
                      )
                    }
                  >
                    加载独立报告
                  </button>
                  {role !== "viewer" && <p>请使用“报告读者”凭证查看。</p>}
                  {report && (
                    <Detail title="独立评价记录" value={report} open />
                  )}
                </>
              )}
            </section>
          </>
        )}
        <footer>Issue Repair Agent · 本地工作台 / 每项结论都对应证据</footer>
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")).render(<App />);
