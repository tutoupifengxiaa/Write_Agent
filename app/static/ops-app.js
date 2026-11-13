(function () {
  const ReactLib = window.React;
  const ReactDOMLib = window.ReactDOM;

  if (!ReactLib || !ReactDOMLib) {
    throw new Error("React runtime failed to load");
  }

  const { useEffect, useRef, useState } = ReactLib;
  const { fetchJson, Navigation, statusLabel, StatusBadge, Feedback } = window.WriteAgentUI;

  function toneForStatus(status) {
    switch (status) {
      case "attention_required":
        return "publish_pending";
      case "failed":
        return "publish_failed";
      default:
        return status || "idle";
    }
  }

  function formatOutcomes(outcomes) {
    if (!Array.isArray(outcomes) || outcomes.length === 0) {
      return "本轮未处理任务。";
    }
    return outcomes
      .map(function (item) {
        return [item.item_type || "任务", item.item_id || "未知", statusLabel(item.final_status)].join(" | ");
      })
      .join(" | ");
  }

  function SummaryStat(props) {
    return ReactLib.createElement(
      "div",
      { className: "ops-stat" },
      ReactLib.createElement("span", null, props.label),
      ReactLib.createElement("strong", null, props.value)
    );
  }

  function App() {
    const [health, setHealth] = useState(null);
    const [summary, setSummary] = useState(null);
    const [runs, setRuns] = useState([]);
    const [jobs, setJobs] = useState([]);
    const [failedJobs, setFailedJobs] = useState([]);
    const [clusters, setClusters] = useState([]);
    const [selectedRun, setSelectedRun] = useState(null);
    const [error, setError] = useState("");
    const [modules, setModules] = useState({});
    const requests = useRef({});
    const runRequest = useRef(0);
    const [selectedRunId, setSelectedRunId] = useState("");
    const [isLoadingRun, setIsLoadingRun] = useState(false);
    const resources = {
      health: { url: "/jobs/health", label: "调度健康", set: setHealth },
      summary: { url: "/jobs/runs/summary", label: "运行汇总", set: setSummary },
      runs: { url: "/jobs/runs?limit=6", label: "调度运行记录", set: setRuns },
      jobs: { url: "/jobs?limit=6", label: "发布任务", set: setJobs },
      failedJobs: { url: "/jobs?status=publish_failed&limit=6", label: "失败发布队列", set: setFailedJobs },
      clusters: { url: "/jobs/runs/failure-clusters", label: "失败聚类", set: setClusters },
    };

    useEffect(function () {
      Object.keys(resources).forEach(loadModule);
    }, []);

    function loadModule(name) {
      const resource = resources[name];
      const requestId = (requests.current[name] || 0) + 1;
      requests.current[name] = requestId;
      setModules(function (current) {
        return Object.assign({}, current, { [name]: { loading: true, error: "" } });
      });
      return fetchJson(resource.url).then(function (value) {
        if (requests.current[name] !== requestId) return;
        resource.set(value);
        if (name === "runs") {
          setSelectedRun(function (current) { return current || value[0] || null; });
        }
        setModules(function (current) {
          return Object.assign({}, current, { [name]: { loading: false, error: "" } });
        });
      }).catch(function (fetchError) {
        if (requests.current[name] !== requestId) return;
        setModules(function (current) {
          return Object.assign({}, current, {
            [name]: { loading: false, error: fetchError.message || "加载失败" },
          });
        });
      });
    }

    function moduleNotice(name) {
      const state = modules[name];
      if (!state || state.loading) {
        return ReactLib.createElement("div", { className: "ops-loading", role: "status" }, "正在加载" + resources[name].label + "…");
      }
      if (!state.error) return null;
      return ReactLib.createElement(Feedback, {
        className: "ops-empty", role: "alert", message: resources[name].label + "加载失败：" + state.error + " ",
      },
        ReactLib.createElement("button", { type: "button", onClick: function () { loadModule(name); } }, "重试"));
    }

    function handleSelectRun(runId) {
      const requestId = ++runRequest.current;
      setSelectedRunId(runId);
      setIsLoadingRun(true);
      setError("");
      fetchJson("/jobs/runs/" + encodeURIComponent(runId))
        .then(function (runDetail) {
          if (requestId === runRequest.current) setSelectedRun(runDetail);
        })
        .catch(function (fetchError) {
          if (requestId === runRequest.current) setError(fetchError.message || "运行详情加载失败");
        }).finally(function () {
          if (requestId === runRequest.current) setIsLoadingRun(false);
        });
    }

    return ReactLib.createElement(
      "main",
      { className: "ops-shell" },
      ReactLib.createElement(
        "section",
        { className: "ops-hero" },
        ReactLib.createElement("p", { className: "ops-eyebrow" }, "Write Agent | 运营工作区"),
        ReactLib.createElement("h1", null, "运营看板"),
        ReactLib.createElement(
          "p",
          null,
          "围绕调度器健康、运行节奏、发布任务和人工接管信号，集中观察这条写作流水线当前是否稳定、是否拥堵、是否需要运营介入。"
        ),
        ReactLib.createElement(Navigation, { current: "/ops" }),
        moduleNotice("health"),
        ReactLib.createElement(
          "div",
          { className: "ops-stats" },
          ReactLib.createElement(SummaryStat, {
            label: "调度状态",
            value: health ? statusLabel(health.scheduler) : "-",
          }),
          ReactLib.createElement(SummaryStat, {
            label: "待创作章节",
            value: health ? health.pending_chapters : "-",
          }),
          ReactLib.createElement(SummaryStat, {
            label: "待发布任务",
            value: health ? health.pending_publish_jobs : "-",
          }),
          ReactLib.createElement(SummaryStat, {
            label: "已处理任务",
            value: summary ? summary.total_processed_items : "-",
          })
        )
      ),
      error
        ? ReactLib.createElement(
            "section",
            { className: "ops-panel", style: { marginTop: "18px" } },
            ReactLib.createElement(Feedback, { className: "ops-empty", role: "alert", message: error }),
            ReactLib.createElement("button", { type: "button", onClick: function () { handleSelectRun(selectedRunId); } }, "重试运行详情")
          )
        : null,
      ReactLib.createElement(
        "section",
        { className: "ops-grid" },
        ReactLib.createElement(
          "div",
          { className: "ops-stack" },
          ReactLib.createElement(
            "section",
            { className: "ops-panel" },
            ReactLib.createElement(
              "div",
              { className: "ops-panel-header" },
              ReactLib.createElement("h2", null, "失败发布队列"),
              ReactLib.createElement("div", { className: "ops-note" }, "需要人工跟进的发布任务")
            ),
            moduleNotice("failedJobs") || (failedJobs.length === 0
              ? ReactLib.createElement("div", { className: "ops-empty" }, "暂无需要人工跟进的发布失败任务。")
              : ReactLib.createElement(
                  "div",
                  { className: "ops-list" },
                  failedJobs.map(function (job) {
                    return ReactLib.createElement(
                      "article",
                      { className: "ops-card", key: job.id },
                      ReactLib.createElement(
                        "div",
                        { className: "ops-card-top" },
                        ReactLib.createElement("strong", { className: "ops-card-title" }, job.chapter_id),
                        ReactLib.createElement(StatusBadge, { className: "ops-chip", "data-tone": toneForStatus(job.status), status: job.status })
                      ),
                      ReactLib.createElement(
                        "div",
                        { className: "ops-card-meta" },
                        ReactLib.createElement("span", { className: "ops-chip" }, job.platform),
                        ReactLib.createElement("span", { className: "ops-chip" }, job.mode),
                        ReactLib.createElement("span", { className: "ops-chip" }, job.created_at || "时间未知（历史记录）")
                      ),
                      ReactLib.createElement(
                        "div",
                        { className: "ops-outcomes" },
                        job.error_message || "未记录错误信息。"
                      )
                    );
                  })
                ))
          ),
          ReactLib.createElement(
            "section",
            { className: "ops-panel" },
            ReactLib.createElement(
              "div",
              { className: "ops-panel-header" },
              ReactLib.createElement("h2", null, "调度运行记录"),
              ReactLib.createElement("div", { className: "ops-note" }, "最近调度轮次")
            ),
            moduleNotice("runs") || ReactLib.createElement(
                  "div",
                  { className: "ops-list" },
                  runs.length === 0
                    ? ReactLib.createElement("div", { className: "ops-empty" }, "暂无调度运行记录。")
                    : runs.map(function (run) {
                        return ReactLib.createElement(
                          "article",
                          {
                            className: "ops-card ops-clickable-card",
                            key: run.id,
                            role: "button",
                            tabIndex: 0,
                            onKeyDown: function (event) {
                              if (event.key === "Enter" || event.key === " ") {
                                event.preventDefault();
                                handleSelectRun(run.id);
                              }
                            },
                            onClick: function () {
                              handleSelectRun(run.id);
                            },
                          },
                          ReactLib.createElement(
                            "div",
                            { className: "ops-card-top" },
                            ReactLib.createElement("strong", { className: "ops-card-title" }, "运行轮次 #" + run.cycle_number),
                            ReactLib.createElement(StatusBadge, { className: "ops-chip", "data-tone": toneForStatus(run.status), status: run.status })
                          ),
                          ReactLib.createElement(
                            "div",
                            { className: "ops-card-meta" },
                            ReactLib.createElement("span", { className: "ops-chip" }, "已处理 " + run.processed_count),
                            ReactLib.createElement("span", { className: "ops-chip" }, run.reason_code || "未知"),
                            ReactLib.createElement("span", { className: "ops-chip" }, String(run.duration_ms || 0) + " ms"),
                            ReactLib.createElement("span", { className: "ops-chip" }, run.created_at || "时间未知")
                          ),
                          ReactLib.createElement("div", { className: "ops-outcomes" }, formatOutcomes(run.outcomes))
                        );
                      })
                )
          ),
          ReactLib.createElement(
            "section",
            { className: "ops-panel" },
            ReactLib.createElement(
              "div",
              { className: "ops-panel-header" },
              ReactLib.createElement("h2", null, "运行详情"),
              ReactLib.createElement("div", { className: "ops-note" }, "查看单轮任务与处理结果")
            ),
            isLoadingRun ? ReactLib.createElement("div", { className: "ops-loading", role: "status" }, "正在加载运行详情…") : !selectedRun
              ? ReactLib.createElement("div", { className: "ops-empty" }, "请选择一条调度记录查看详情。")
              : ReactLib.createElement(
                  "div",
                  { className: "ops-detail-grid" },
                  ReactLib.createElement(
                    "div",
                    { className: "ops-card" },
                    ReactLib.createElement("strong", { className: "ops-card-title" }, "运行轮次 #" + selectedRun.cycle_number),
                    ReactLib.createElement(
                      "div",
                      { className: "ops-card-meta" },
                      ReactLib.createElement(StatusBadge, { className: "ops-chip", "data-tone": toneForStatus(selectedRun.status), status: selectedRun.status }),
                      ReactLib.createElement("span", { className: "ops-chip" }, "已处理 " + selectedRun.processed_count),
                      ReactLib.createElement("span", { className: "ops-chip" }, selectedRun.reason_code || "未知"),
                      ReactLib.createElement("span", { className: "ops-chip" }, String(selectedRun.duration_ms || 0) + " ms"),
                      ReactLib.createElement("span", { className: "ops-chip" }, selectedRun.created_at || "时间未知")
                    )
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "ops-list" },
                    (selectedRun.outcomes || []).length === 0
                      ? ReactLib.createElement("div", { className: "ops-empty" }, "本轮暂无处理结果。")
                      : selectedRun.outcomes.map(function (outcome, index) {
                          return ReactLib.createElement(
                            "article",
                            { className: "ops-card", key: index },
                            ReactLib.createElement(
                              "div",
                              { className: "ops-card-top" },
                              ReactLib.createElement("strong", { className: "ops-card-title" }, outcome.item_id || "未知任务"),
                              ReactLib.createElement(StatusBadge, { className: "ops-chip", "data-tone": toneForStatus(outcome.final_status), status: outcome.final_status })
                            ),
                            ReactLib.createElement(
                              "div",
                              { className: "ops-card-meta" },
                              ReactLib.createElement("span", { className: "ops-chip" }, outcome.item_type || "任务"),
                              outcome.error_type
                                ? ReactLib.createElement("span", { className: "ops-chip" }, outcome.error_type)
                                : null
                            )
                          );
                        })
                  )
                )
          )
        ),
        ReactLib.createElement(
          "div",
          { className: "ops-stack" },
          ReactLib.createElement(
            "section",
            { className: "ops-panel" },
            ReactLib.createElement(
              "div",
              { className: "ops-panel-header" },
              ReactLib.createElement("h2", null, "运行汇总"),
              ReactLib.createElement("div", { className: "ops-note" }, "调度运行累计情况")
            ),
            moduleNotice("summary") || (summary ? ReactLib.createElement(
                  "div",
                  { className: "ops-list" },
                  ReactLib.createElement(
                    "article",
                    { className: "ops-card" },
                    ReactLib.createElement(
                      "div",
                      { className: "ops-card-meta" },
                      ReactLib.createElement("span", { className: "ops-chip" }, "总轮次 " + summary.total_runs),
                      ReactLib.createElement(StatusBadge, { className: "ops-chip", "data-tone": "processed" }, "已处理 " + summary.processed_runs),
                      ReactLib.createElement(StatusBadge, { className: "ops-chip", "data-tone": "idle" }, "空闲轮次 " + summary.idle_runs)
                    )
                  )
                ) : null)
          ),
          ReactLib.createElement(
            "section",
            { className: "ops-panel" },
            ReactLib.createElement(
              "div",
              { className: "ops-panel-header" },
              ReactLib.createElement("h2", null, "失败聚类"),
              ReactLib.createElement("div", { className: "ops-note" }, "重复出现的失败类型")
            ),
            moduleNotice("clusters") || (clusters.length === 0
              ? ReactLib.createElement("div", { className: "ops-empty" }, "暂无重复失败记录。")
              : ReactLib.createElement(
                  "div",
                  { className: "ops-cluster-list" },
                  clusters.map(function (cluster) {
                    return ReactLib.createElement(
                      "article",
                      { className: "ops-card", key: cluster.failure_key },
                      ReactLib.createElement(
                        "div",
                        { className: "ops-card-top" },
                        ReactLib.createElement("strong", { className: "ops-card-title" }, statusLabel(cluster.final_status)),
                        ReactLib.createElement(StatusBadge, { className: "ops-chip", "data-tone": toneForStatus(cluster.final_status) }, "x" + cluster.count)
                      ),
                      ReactLib.createElement(
                        "div",
                        { className: "ops-card-meta" },
                        ReactLib.createElement("span", { className: "ops-chip" }, cluster.item_type),
                        ReactLib.createElement("span", { className: "ops-chip" }, cluster.error_type),
                        ReactLib.createElement("span", { className: "ops-chip" }, "最近轮次 " + cluster.latest_cycle_number)
                      ),
                      ReactLib.createElement("div", { className: "ops-outcomes" }, "示例任务：" + cluster.sample_item_id)
                    );
                  })
                ))
          ),
          ReactLib.createElement(
            "section",
            { className: "ops-panel" },
            ReactLib.createElement(
              "div",
              { className: "ops-panel-header" },
              ReactLib.createElement("h2", null, "发布任务"),
              ReactLib.createElement("div", { className: "ops-note" }, "最近发布任务")
            ),
            moduleNotice("jobs") || (jobs.length === 0
              ? ReactLib.createElement("div", { className: "ops-empty" }, "暂无发布任务。")
              : ReactLib.createElement(
                  "div",
                  { className: "ops-list" },
                  jobs.map(function (job) {
                    return ReactLib.createElement(
                      "article",
                      { className: "ops-card", key: job.id },
                      ReactLib.createElement(
                        "div",
                        { className: "ops-card-top" },
                        ReactLib.createElement("strong", { className: "ops-card-title" }, job.chapter_id),
                        ReactLib.createElement(StatusBadge, { className: "ops-chip", "data-tone": toneForStatus(job.status), status: job.status })
                      ),
                      ReactLib.createElement(
                        "div",
                        { className: "ops-card-meta" },
                        ReactLib.createElement("span", { className: "ops-chip" }, job.platform),
                        ReactLib.createElement("span", { className: "ops-chip" }, job.mode),
                        ReactLib.createElement("span", { className: "ops-chip" }, job.created_at || "时间未知（历史记录）"),
                        job.platform_draft_id
                          ? ReactLib.createElement("span", { className: "ops-chip" }, "平台草稿 " + job.platform_draft_id)
                          : null
                      ),
                      job.error_message
                        ? ReactLib.createElement("div", { className: "ops-outcomes" }, job.error_message)
                        : null
                    );
                  })
                ))
          )
        )
      )
    );
  }

  const rootElement = document.getElementById("ops-root");
  ReactDOMLib.createRoot(rootElement).render(ReactLib.createElement(App));
})();
