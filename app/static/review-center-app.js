(function () {
  const ReactLib = window.React;
  const ReactDOMLib = window.ReactDOM;
  const { fetchJson, requestJson, Navigation, statusLabel } = window.WriteAgentUI;

  if (!ReactLib || !ReactDOMLib) {
    throw new Error("页面运行资源加载失败，请刷新重试");
  }

  const { useEffect, useMemo, useState } = ReactLib;

  function buildCreationLink(bookId, chapterId) {
    return "/creation?book_id=" + encodeURIComponent(bookId || "") + "&chapter_id=" + encodeURIComponent(chapterId || "");
  }

  function buildDetailLink(chapterId) {
    return "/chapters/" + encodeURIComponent(chapterId) + "/detail";
  }

  function candidateReviewTrace(item) {
    if (
      item.candidate_version &&
      item.candidate_version.generation_metadata &&
      item.candidate_version.generation_metadata.review_trace
    ) {
      return item.candidate_version.generation_metadata.review_trace;
    }
    return item.goal && item.goal.review_trace ? item.goal.review_trace : null;
  }

  function reviewIssues(trace) {
    if (!trace || typeof trace !== "object") {
      return [];
    }
    const initialIssues = trace.initial_review && Array.isArray(trace.initial_review.issues)
      ? trace.initial_review.issues
      : [];
    const finalIssues = trace.final_review && Array.isArray(trace.final_review.issues)
      ? trace.final_review.issues
      : [];
    const finalById = {};
    finalIssues.forEach(function (issue) {
      if (issue && issue.id) {
        finalById[issue.id] = issue;
      }
    });
    const seen = {};
    return initialIssues.concat(finalIssues).map(function (issue) {
      return issue && issue.id && finalById[issue.id] ? finalById[issue.id] : issue;
    }).filter(function (issue) {
      const issueId = issue && issue.id;
      if (!issueId || seen[issueId]) {
        return false;
      }
      seen[issueId] = true;
      return true;
    });
  }

  function issueResolution(trace, issueId) {
    const revision = trace && trace.revision && typeof trace.revision === "object" ? trace.revision : {};
    const finalIssueIds = trace && trace.final_review && Array.isArray(trace.final_review.issues)
      ? trace.final_review.issues.map(function (issue) {
          return issue && issue.id;
        }).filter(Boolean)
      : [];
    if (finalIssueIds.indexOf(issueId) >= 0) {
      return "未解决";
    }
    const resolvedIssueIds = []
      .concat(Array.isArray(revision.resolved_issue_ids) ? revision.resolved_issue_ids : [])
      .concat(Array.isArray(trace && trace.resolved_issue_ids) ? trace.resolved_issue_ids : []);
    const unresolvedIssueIds = []
      .concat(Array.isArray(revision.unresolved_issue_ids) ? revision.unresolved_issue_ids : [])
      .concat(Array.isArray(trace && trace.unresolved_issue_ids) ? trace.unresolved_issue_ids : []);
    if (unresolvedIssueIds.indexOf(issueId) >= 0) {
      return "未解决";
    }
    if (resolvedIssueIds.indexOf(issueId) >= 0) {
      return "已解决";
    }
    return "待确认";
  }

  function renderReviewAudit(item) {
    const trace = candidateReviewTrace(item);
    const issues = reviewIssues(trace);
    const candidateContent = item.candidate_version && item.candidate_version.content;

    return ReactLib.createElement(
      "div",
      { className: "review-card-note", "data-audit-section": "review-trace" },
      ReactLib.createElement("strong", null, "审核候选正文"),
      ReactLib.createElement("p", null, candidateContent || "暂无候选正文"),
      !trace
        ? ReactLib.createElement("p", null, "暂无可用审核追踪")
        : issues.length === 0
          ? ReactLib.createElement("p", null, "审核追踪中暂无问题记录")
          : ReactLib.createElement(
              "div",
              { className: "review-side-list" },
              issues.map(function (issue) {
                return ReactLib.createElement(
                  "div",
                  { key: issue.id, className: "review-card-note" },
                  ReactLib.createElement("strong", null, issue.id),
                  ReactLib.createElement(
                    "div",
                    { className: "review-card-actions" },
                    ReactLib.createElement("span", { className: "review-chip", "data-tone": issue.severity }, issue.type || "未分类"),
                    ReactLib.createElement("span", { className: "review-chip", "data-tone": issue.severity }, issue.severity || "未分级"),
                    ReactLib.createElement("span", { className: "review-chip" }, issue.source || "来源未知"),
                    ReactLib.createElement("span", { className: "review-chip" }, issueResolution(trace, issue.id))
                  ),
                  ReactLib.createElement("p", null, issue.message || "未记录问题说明"),
                  ReactLib.createElement("p", null, "证据：" + (issue.evidence || "未记录证据"))
                );
              })
            )
    );
  }

  function App() {
    const [queue, setQueue] = useState([]);
    const [error, setError] = useState("");
    const [success, setSuccess] = useState("");
    const [submittingId, setSubmittingId] = useState("");
    const [noteMap, setNoteMap] = useState({});
    const [handoff, setHandoff] = useState(null);

    function loadQueue() {
      return fetchJson("/chapters/manual-review-queue")
        .then(function (rows) {
          setQueue(rows);
          const nextNoteMap = {};
          rows.forEach(function (item) {
            nextNoteMap[item.id] = item.manual_review_note || "";
          });
          setNoteMap(nextNoteMap);
        })
        .catch(function (fetchError) {
          setError(fetchError.message || "审核队列加载失败");
        });
    }

    function refreshHandoffJob(publishJobId) {
      return requestJson("/jobs/" + publishJobId)
        .then(function (job) {
          setHandoff(function (current) {
            if (!current || current.publishJobId !== publishJobId) {
              return current;
            }
            return Object.assign({}, current, {
              status: job.status,
              errorMessage: job.error_message || "",
            });
          });
        })
        .catch(function () {
          return null;
        });
    }

    useEffect(function () {
      loadQueue();
    }, []);

    useEffect(function () {
      if (!handoff || !handoff.publishJobId) {
        return undefined;
      }

      refreshHandoffJob(handoff.publishJobId);
      const timer = window.setInterval(function () {
        refreshHandoffJob(handoff.publishJobId);
      }, 4000);

      return function () {
        window.clearInterval(timer);
      };
    }, [handoff && handoff.publishJobId]);

    const summary = useMemo(function () {
      return {
        total: queue.length,
        reviewPending: queue.filter(function (item) {
          return item.status === "review_pending";
        }).length,
        revisionPending: queue.filter(function (item) {
          return item.status === "revision_pending";
        }).length,
      };
    }, [queue]);

    function updateNote(chapterId, value) {
      setNoteMap(function (current) {
        return Object.assign({}, current, { [chapterId]: value });
      });
    }

    function submitApprove(chapterId, createPublishJob) {
      setError("");
      setSuccess("");
      setSubmittingId(chapterId + (createPublishJob ? ":approve-publish" : ":approve"));
      requestJson("/api/review-center/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chapter_id: chapterId,
          note: noteMap[chapterId] || "",
          create_publish_job: createPublishJob,
        }),
      })
        .then(function (payload) {
          if (payload.publish_job_id) {
            setSuccess("已通过审核并加入发布队列。");
            setHandoff({
              chapterId: payload.chapter_id,
              publishJobId: payload.publish_job_id,
              status: "publish_pending",
              errorMessage: "",
            });
          } else {
            setSuccess("已通过审核并完成入库。");
            setHandoff(null);
          }
          return loadQueue();
        })
        .catch(function (requestError) {
          setError(requestError.message || "人工审核通过失败");
        })
        .finally(function () {
          setSubmittingId("");
        });
    }

    function submitRevise(chapterId) {
      setError("");
      setSuccess("");
      setHandoff(null);
      setSubmittingId(chapterId + ":revise");
      requestJson("/api/review-center/revise", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chapter_id: chapterId,
          note: noteMap[chapterId] || "",
        }),
      })
        .then(function () {
          setSuccess("已退回待修订。");
          return loadQueue();
        })
        .catch(function (requestError) {
          setError(requestError.message || "退回待修订失败");
        })
        .finally(function () {
          setSubmittingId("");
        });
    }

    function retryFailedPublish(chapterId) {
      setError("");
      setSuccess("");
      setSubmittingId(chapterId + ":retry-publish");
      requestJson("/chapters/" + chapterId + "/retry-publish", {
        method: "POST",
      })
        .then(function () {
          setSuccess("已重新加入发布队列。");
          setHandoff(function (current) {
            if (!current || current.chapterId !== chapterId) {
              return current;
            }
            return Object.assign({}, current, {
              status: "publish_pending",
              errorMessage: "",
            });
          });
        })
        .catch(function (requestError) {
          setError(requestError.message || "重试发布失败");
        })
        .finally(function () {
          setSubmittingId("");
        });
    }

    function archiveFailedPublish(chapterId) {
      setError("");
      setSuccess("");
      setSubmittingId(chapterId + ":archive-publish");
      requestJson("/chapters/" + chapterId + "/archive-publish-failure", {
        method: "POST",
      })
        .then(function () {
          setSuccess("已归档失败发布任务。");
          setHandoff(function (current) {
            if (!current || current.chapterId !== chapterId) {
              return current;
            }
            return Object.assign({}, current, {
              status: "archived",
            });
          });
        })
        .catch(function (requestError) {
          setError(requestError.message || "归档失败发布任务失败");
        })
        .finally(function () {
          setSubmittingId("");
        });
    }

    return ReactLib.createElement(
      "main",
      { className: "review-shell" },
      ReactLib.createElement(
        "section",
        { className: "review-hero" },
        ReactLib.createElement("p", { className: "review-eyebrow" }, "Write Agent · 人工审核"),
        ReactLib.createElement("h1", null, "人工审核台"),
        ReactLib.createElement(
          "p",
          null,
          "集中查看待人工处理章节，把连续性校对、修订建议和通过入库动作收拢到一个真实工作台。"
        ),
        ReactLib.createElement(Navigation, { current: "/review-center" }),
        ReactLib.createElement(
          "div",
          { className: "review-summary-grid" },
          ReactLib.createElement("div", { className: "review-summary-card" }, ReactLib.createElement("span", null, "待人工处理章节"), ReactLib.createElement("strong", null, String(summary.total))),
          ReactLib.createElement("div", { className: "review-summary-card" }, ReactLib.createElement("span", null, "连续性待审核"), ReactLib.createElement("strong", null, String(summary.reviewPending))),
          ReactLib.createElement("div", { className: "review-summary-card" }, ReactLib.createElement("span", null, "待按建议修订"), ReactLib.createElement("strong", null, String(summary.revisionPending)))
        )
      ),
      error ? ReactLib.createElement("div", { className: "review-feedback review-error", role: "alert" }, error) : null,
      success ? ReactLib.createElement("div", { className: "review-feedback review-success" }, success) : null,
      handoff
        ? ReactLib.createElement(
            "section",
            { className: "review-handoff-card" },
            ReactLib.createElement("span", { className: "review-handoff-label" }, "发布交接"),
            ReactLib.createElement("strong", null, "该章节已进入发布前队列"),
            ReactLib.createElement(
              "div",
              { className: "review-handoff-meta" },
              ReactLib.createElement("span", null, "发布任务：" + handoff.publishJobId),
              ReactLib.createElement("span", { className: "review-handoff-status" }, "状态：" + statusLabel(handoff.status))
            ),
            handoff.errorMessage
              ? ReactLib.createElement("div", { className: "review-handoff-error" }, handoff.errorMessage)
              : null,
            ReactLib.createElement(
              "div",
              { className: "review-handoff-actions" },
              ReactLib.createElement("a", { href: "/library" }, "去文章管理看状态"),
              ReactLib.createElement("a", { href: "/ops", className: "secondary" }, "去运营看板跟进"),
              ReactLib.createElement("a", { href: buildDetailLink(handoff.chapterId), className: "secondary" }, "看章节详情")
            ),
            handoff.status === "publish_failed"
              ? ReactLib.createElement(
                  "div",
                  { className: "review-handoff-actions" },
                  ReactLib.createElement(
                    "button",
                    {
                      type: "button",
                      className: "review-action-button",
                      disabled: submittingId === handoff.chapterId + ":retry-publish",
                      onClick: function () {
                        retryFailedPublish(handoff.chapterId);
                      },
                    },
                    submittingId === handoff.chapterId + ":retry-publish" ? "重试中..." : "重试发布"
                  ),
                  ReactLib.createElement(
                    "button",
                    {
                      type: "button",
                      className: "review-action-button secondary",
                      disabled: submittingId === handoff.chapterId + ":archive-publish",
                      onClick: function () {
                        archiveFailedPublish(handoff.chapterId);
                      },
                    },
                    submittingId === handoff.chapterId + ":archive-publish" ? "归档中..." : "归档失败"
                  )
                )
              : null
          )
        : null,
      ReactLib.createElement(
        "section",
        { className: "review-layout" },
        ReactLib.createElement(
          "section",
          { className: "review-panel" },
          ReactLib.createElement("h2", null, "待人工处理章节"),
          queue.length === 0
            ? ReactLib.createElement("div", { className: "review-empty" }, "当前没有待人工处理章节。")
            : ReactLib.createElement(
                "div",
                { className: "review-queue-grid" },
                queue.map(function (item) {
                  const approveBusy = submittingId === item.id + ":approve";
                  const approvePublishBusy = submittingId === item.id + ":approve-publish";
                  const reviseBusy = submittingId === item.id + ":revise";
                  return ReactLib.createElement(
                    "article",
                    { className: "review-queue-card", key: item.id },
                    ReactLib.createElement(
                      "div",
                      { className: "review-card-top" },
                      ReactLib.createElement(
                        "div",
                        { className: "review-card-meta" },
                        ReactLib.createElement("strong", null, item.title),
                        ReactLib.createElement("span", null, item.book_title + " · 第 " + item.chapter_number + " 章"),
                        ReactLib.createElement("em", null, item.goal && item.goal.objective ? item.goal.objective : "未填写章节目标")
                      ),
                      ReactLib.createElement("span", { className: "review-chip", "data-tone": item.status }, statusLabel(item.status))
                    ),
                    ReactLib.createElement("div", { className: "review-card-note" }, item.manual_review_note || "暂无人工备注"),
                    renderReviewAudit(item),
                    ReactLib.createElement("label", { className: "review-note-label", htmlFor: "review-note-" + item.id }, "审核备注"),
                    ReactLib.createElement("textarea", {
                      id: "review-note-" + item.id,
                      className: "review-note-input",
                      value: noteMap[item.id] || "",
                      onChange: function (event) {
                        updateNote(item.id, event.target.value);
                      },
                    }),
                    ReactLib.createElement(
                      "div",
                      { className: "review-card-actions" },
                      ReactLib.createElement("a", { href: buildCreationLink(item.book_id, item.id) }, "继续处理"),
                      ReactLib.createElement("a", { href: buildDetailLink(item.id), className: "secondary" }, "章节详情"),
                      ReactLib.createElement(
                        "button",
                        {
                          type: "button",
                          className: "review-action-button secondary",
                          disabled: reviseBusy || approveBusy || approvePublishBusy,
                          onClick: function () {
                            submitRevise(item.id);
                          },
                        },
                        reviseBusy ? "提交中..." : "退回待修订"
                      ),
                      ReactLib.createElement(
                        "button",
                        {
                          type: "button",
                          className: "review-action-button",
                          disabled: reviseBusy || approveBusy || approvePublishBusy,
                          onClick: function () {
                            submitApprove(item.id, false);
                          },
                        },
                        approveBusy ? "入库中..." : "通过入库"
                      ),
                      ReactLib.createElement(
                        "button",
                        {
                          type: "button",
                          className: "review-action-button publish",
                          disabled: reviseBusy || approveBusy || approvePublishBusy,
                          onClick: function () {
                            submitApprove(item.id, true);
                          },
                        },
                        approvePublishBusy ? "排队中..." : "通过并排队发布"
                      )
                    )
                  );
                })
              )
        ),
        ReactLib.createElement(
          "aside",
          { className: "review-panel" },
          ReactLib.createElement("h2", null, "处理建议"),
          ReactLib.createElement(
            "ul",
            { className: "review-side-list" },
            ReactLib.createElement("li", null, "先看备注，再决定是回创作台补稿，还是直接审核通过。"),
            ReactLib.createElement("li", null, "通过入库适合先沉淀版本，再由其他页面决定是否发布。"),
            ReactLib.createElement("li", null, "通过并排队发布适合已经确认质量达标、可以直接进入番茄草稿队列的章节。")
          )
        )
      )
    );
  }

  const rootElement = document.getElementById("review-center-root");
  ReactDOMLib.createRoot(rootElement).render(ReactLib.createElement(App));
})();
