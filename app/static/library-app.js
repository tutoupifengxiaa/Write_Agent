(function () {
  const ReactLib = window.React;
  const ReactDOMLib = window.ReactDOM;

  if (!ReactLib || !ReactDOMLib) {
    throw new Error("React runtime failed to load");
  }

  const { useEffect, useRef, useState } = ReactLib;
  const { fetchJson, requestJson, Navigation, statusLabel, FormField, StatusBadge, Feedback } = window.WriteAgentUI;

  function getStatusTone(status) {
    return status || "idle";
  }

  function renderStyleRules(styleRules) {
    if (!styleRules || typeof styleRules !== "object") {
      return "未配置";
    }
    return Object.keys(styleRules)
      .map(function (key) {
        return key + ": " + styleRules[key];
      })
      .join(" | ");
  }

  function buildCreationLink(bookId, chapterId) {
    return "/creation?book_id=" + encodeURIComponent(bookId || "") + "&chapter_id=" + encodeURIComponent(chapterId || "");
  }

  function App() {
    const bookRequest = useRef(0);
    const [isLoadingBook, setIsLoadingBook] = useState(false);
    const [books, setBooks] = useState([]);
    const [selectedBook, setSelectedBook] = useState(null);
    const [chapters, setChapters] = useState([]);
    const [jobMap, setJobMap] = useState({});
    const [selectedChapterId, setSelectedChapterId] = useState("");
    const [chapterStatusFilter, setChapterStatusFilter] = useState("all");
    const [chapterSearch, setChapterSearch] = useState("");
    const [error, setError] = useState("");
    const [success, setSuccess] = useState("");
    const [isSubmitting, setIsSubmitting] = useState(false);
    const [bookEdit, setBookEdit] = useState(null);
    const [chapterEdit, setChapterEdit] = useState(null);
    const [chapterForm, setChapterForm] = useState({
      chapterNumber: "",
      title: "",
      objective: "",
    });

    useEffect(function () {
      fetchJson("/books")
        .then(function (rows) {
          setBooks(rows);
          if (rows.length > 0) {
            return loadBook(rows[0].id);
          }
          return null;
        })
        .catch(function (fetchError) {
          setError(fetchError.message || "书籍列表加载失败");
        });
    }, []);

    // Close edit forms when the selection changes, so a save never targets a different item.
    useEffect(function () { setBookEdit(null); }, [selectedBook && selectedBook.id]);
    useEffect(function () { setChapterEdit(null); }, [selectedChapterId]);

    function resetFeedback() {
      setError("");
      setSuccess("");
    }

    function updateChapterForm(name, value) {
      setChapterForm(function (current) {
        return Object.assign({}, current, { [name]: value });
      });
    }

    function loadBook(bookId) {
      const requestId = ++bookRequest.current;
      setIsLoadingBook(true);
      setSelectedBook(null);
      setChapters([]);
      setJobMap({});
      const encodedId = encodeURIComponent(bookId);
      return Promise.all([
        fetchJson("/books/" + encodedId),
        fetchJson("/books/" + encodedId + "/chapters"),
        fetchJson("/jobs?book_id=" + encodedId + "&latest_per_chapter=true"),
      ]).then(function (values) {
        if (requestId !== bookRequest.current) return;
        setSelectedBook(values[0]);
        setChapters(values[1]);
        const nextMap = {};
        values[2].forEach(function (job) { nextMap[job.chapter_id] = job; });
        setJobMap(nextMap);
        setChapterSearch("");
        const nextSelectedChapterId = values[1].length > 0 ? values[1][0].id : "";
        setSelectedChapterId(nextSelectedChapterId);
        setChapterForm(function (current) {
          return Object.assign({}, current, {
            chapterNumber: values[1].length > 0 ? String(values[1][values[1].length - 1].chapter_number + 1) : "1",
          });
        });
      }).catch(function (fetchError) {
        if (requestId === bookRequest.current) setError(fetchError.message || "书籍加载失败");
      }).finally(function () {
        if (requestId === bookRequest.current) setIsLoadingBook(false);
      });
    }

    function selectBook(bookId) {
      resetFeedback();
      loadBook(bookId).catch(function (fetchError) {
        setError(fetchError.message || "书籍加载失败");
      });
    }

    function handleCreateChapter() {
      if (!selectedBook) {
        return;
      }
      resetFeedback();
      setIsSubmitting(true);
      requestJson("/chapters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          book_id: selectedBook.id,
          chapter_number: Number(chapterForm.chapterNumber || 0),
          title: chapterForm.title,
          goal: { objective: chapterForm.objective },
        }),
      })
        .then(function () {
          setSuccess("已创建新章节");
          setChapterForm(function (current) {
            return Object.assign({}, current, { title: "", objective: "" });
          });
          return loadBook(selectedBook.id);
        })
        .catch(function (requestError) {
          setError(requestError.message || "章节创建失败");
        })
        .finally(function () {
          setIsSubmitting(false);
        });
    }

    function handleRetryPublish(chapterId) {
      resetFeedback();
      setIsSubmitting(true);
      requestJson("/chapters/" + encodeURIComponent(chapterId) + "/retry-publish", {
        method: "POST",
      })
        .then(function () {
          setSuccess("已重新加入发布队列");
          return loadBook(selectedBook.id);
        })
        .catch(function (requestError) {
          setError(requestError.message || "发布重试失败");
        })
        .finally(function () {
          setIsSubmitting(false);
        });
    }

    function handleArchiveFailure(chapterId) {
      resetFeedback();
      setIsSubmitting(true);
      requestJson("/chapters/" + encodeURIComponent(chapterId) + "/archive-publish-failure", {
        method: "POST",
      })
        .then(function () {
          setSuccess("已归档失败发布任务");
          return loadBook(selectedBook.id);
        })
        .catch(function (requestError) {
          setError(requestError.message || "发布失败任务归档失败");
        })
        .finally(function () {
          setIsSubmitting(false);
        });
    }

    function runAction(request, successText, failText, after) {
      resetFeedback();
      setIsSubmitting(true);
      return request()
        .then(function () {
          setSuccess(successText);
          return after ? after() : null;
        })
        .catch(function (requestError) {
          setError(requestError.message || failText);
        })
        .finally(function () {
          setIsSubmitting(false);
        });
    }

    function sendJson(url, method, body) {
      return requestJson(url, {
        method: method,
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    }

    function reloadBooks(nextBookId) {
      return fetchJson("/books").then(function (rows) {
        setBooks(rows);
        const target = rows.find(function (row) { return row.id === nextBookId; }) || rows[0];
        if (target) return loadBook(target.id);
        setSelectedBook(null);
        setChapters([]);
        setSelectedChapterId("");
        return null;
      });
    }

    function handleSaveBook() {
      const bookId = selectedBook.id;
      runAction(function () {
        return sendJson("/books/" + encodeURIComponent(bookId), "PATCH", bookEdit);
      }, "书籍信息已更新", "书籍信息保存失败", function () {
        setBookEdit(null);
        return reloadBooks(bookId);
      });
    }

    function handleDeleteBook() {
      const book = selectedBook;
      const confirmed = window.confirm(
        "将永久删除《" + book.title + "》及其全部 " + chapters.length +
        " 个章节、所有版本、角色记录和发布任务。此操作无法恢复，确定删除？"
      );
      if (!confirmed) return;
      runAction(function () {
        return sendJson("/books/" + encodeURIComponent(book.id), "DELETE");
      }, "已删除《" + book.title + "》", "删除书籍失败", function () {
        setBookEdit(null);
        return reloadBooks(null);
      });
    }

    function handleSaveChapter() {
      const chapterId = selectedChapter.id;
      runAction(function () {
        return sendJson("/chapters/" + encodeURIComponent(chapterId), "PATCH", {
          title: chapterEdit.title,
          chapter_number: Number(chapterEdit.chapterNumber || 0),
        });
      }, "章节信息已更新", "章节信息保存失败", function () {
        setChapterEdit(null);
        return loadBook(selectedBook.id).then(function () { setSelectedChapterId(chapterId); });
      });
    }

    function handleDeleteChapter() {
      const chapter = selectedChapter;
      const confirmed = window.confirm(
        "将永久删除第 " + chapter.chapter_number + " 章《" + chapter.title +
        "》及其所有版本、发布任务和角色记录。此操作无法恢复，确定删除？"
      );
      if (!confirmed) return;
      runAction(function () {
        return sendJson("/chapters/" + encodeURIComponent(chapter.id), "DELETE");
      }, "已删除第 " + chapter.chapter_number + " 章", "删除章节失败", function () {
        setChapterEdit(null);
        return loadBook(selectedBook.id);
      });
    }

    function handleExport(format) {
      resetFeedback();
      setIsSubmitting(true);
      fetch("/books/" + encodeURIComponent(selectedBook.id) + "/export?format=" + format)
        .then(function (response) {
          if (!response.ok) {
            return response.json().catch(function () { return {}; }).then(function (payload) {
              throw new Error(typeof payload.detail === "string" ? payload.detail : "导出失败（" + response.status + "）");
            });
          }
          const disposition = response.headers.get("Content-Disposition") || "";
          const match = /filename\*=UTF-8''([^;]+)/.exec(disposition);
          const filename = match ? decodeURIComponent(match[1]) : selectedBook.title + "." + format;
          return response.blob().then(function (blob) {
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.href = url;
            link.download = filename;
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(url);
            setSuccess("已导出 " + filename);
          });
        })
        .catch(function (requestError) {
          setError(requestError.message || "导出失败");
        })
        .finally(function () {
          setIsSubmitting(false);
        });
    }

    function handleRequeueGeneration(chapterId) {
      resetFeedback();
      setIsSubmitting(true);
      requestJson("/chapters/" + encodeURIComponent(chapterId) + "/requeue-generation", {
        method: "POST",
      })
        .then(function () {
          setSuccess("已重新加入生成队列，后台 Worker 会重新生成本章");
          return loadBook(selectedBook.id);
        })
        .catch(function (requestError) {
          setError(requestError.message || "重新排队失败");
        })
        .finally(function () {
          setIsSubmitting(false);
        });
    }

    const filteredChapters = chapters.filter(function (chapter) {
      const matchesStatus = chapterStatusFilter === "all" || chapter.status === chapterStatusFilter;
      const matchesSearch =
        !chapterSearch ||
        chapter.title.toLowerCase().includes(chapterSearch.toLowerCase()) ||
        String(chapter.chapter_number).includes(chapterSearch);
      return matchesStatus && matchesSearch;
    });

    const selectedChapter = chapters.find(function (item) {
      return item.id === selectedChapterId;
    }) || null;

    const selectedChapterJob = selectedChapter ? jobMap[selectedChapter.id] : null;

    return ReactLib.createElement(
      "main",
      { className: "library-shell" },
      ReactLib.createElement(
        "section",
        { className: "library-hero" },
        ReactLib.createElement("p", { className: "library-eyebrow" }, "Write Agent | 文章工作区"),
        ReactLib.createElement("h1", null, "文章管理"),
        ReactLib.createElement(
          "p",
          null,
          "集中查看书籍状态、章节进度、大纲沉淀和创作入口，作为单本书持续运营的管理台。"
        ),
        ReactLib.createElement(Navigation, { current: "/library" })
      ),
      ReactLib.createElement(Feedback, { className: "library-error", message: error }),
      ReactLib.createElement(Feedback, { className: "library-success", message: success }),
      isLoadingBook ? ReactLib.createElement("div", { className: "library-empty", role: "status" }, "正在加载书籍…") : null,
      ReactLib.createElement(
        "section",
        { className: "library-grid" },
        ReactLib.createElement(
          "aside",
          { className: "library-panel library-sidebar" },
          ReactLib.createElement("h2", null, "书籍列表"),
          books.length === 0
            ? ReactLib.createElement("div", { className: "library-empty" }, "还没有书籍记录。")
            : books.map(function (book) {
                return ReactLib.createElement(
                  "button",
                  {
                    key: book.id,
                    type: "button",
                    disabled: isSubmitting,
                    className: selectedBook && selectedBook.id === book.id ? "library-book-card active" : "library-book-card",
                    onClick: function () {
                      selectBook(book.id);
                    },
                  },
                  ReactLib.createElement("strong", null, book.title),
                  ReactLib.createElement("span", null, book.platform + " | " + book.genre),
                  ReactLib.createElement(StatusBadge, { as: "em", className: "library-chip", "data-tone": getStatusTone(book.status), status: book.status })
                );
              })
        ),
        ReactLib.createElement(
          "div",
          { className: "library-main" },
          ReactLib.createElement(
            "section",
            { className: "library-panel" },
            ReactLib.createElement("h2", null, selectedBook ? selectedBook.title : "书籍概览"),
            selectedBook
              ? ReactLib.createElement(
                  "div",
                  { className: "library-overview-grid" },
                  ReactLib.createElement(
                    "div",
                    { className: "library-summary-card" },
                    ReactLib.createElement("span", { className: "library-label" }, "平台"),
                    ReactLib.createElement("strong", null, selectedBook.platform || "-")
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "library-summary-card" },
                    ReactLib.createElement("span", { className: "library-label" }, "题材"),
                    ReactLib.createElement("strong", null, selectedBook.genre || "-")
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "library-summary-card" },
                    ReactLib.createElement("span", { className: "library-label" }, "状态"),
                    ReactLib.createElement("strong", null, statusLabel(selectedBook.status))
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "library-summary-card" },
                    ReactLib.createElement("span", { className: "library-label" }, "章节数"),
                    ReactLib.createElement("strong", null, String(chapters.length))
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "library-summary-card library-summary-card-wide" },
                    ReactLib.createElement("span", { className: "library-label" }, "故事梗概"),
                    ReactLib.createElement("strong", null, selectedBook.logline || "暂未生成")
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "library-summary-card library-summary-card-wide" },
                    ReactLib.createElement("span", { className: "library-label" }, "风格规则"),
                    ReactLib.createElement("strong", null, renderStyleRules(selectedBook.style_rules))
                  )
                )
              : ReactLib.createElement("div", { className: "library-empty" }, "请选择一本书查看详情。"),
            selectedBook
              ? ReactLib.createElement(
                  "div",
                  { className: "library-button-row" },
                  ReactLib.createElement(
                    "button",
                    { type: "button", className: "library-secondary-button", disabled: isSubmitting,
                      onClick: function () { handleExport("txt"); } },
                    "导出 TXT"
                  ),
                  ReactLib.createElement(
                    "button",
                    { type: "button", className: "library-secondary-button", disabled: isSubmitting,
                      onClick: function () { handleExport("md"); } },
                    "导出 Markdown"
                  ),
                  ReactLib.createElement(
                    "button",
                    { type: "button", className: "library-secondary-button", disabled: isSubmitting,
                      onClick: function () {
                        setBookEdit(bookEdit ? null : {
                          title: selectedBook.title, platform: selectedBook.platform, genre: selectedBook.genre,
                        });
                      } },
                    bookEdit ? "取消编辑" : "编辑信息"
                  ),
                  ReactLib.createElement(
                    "button",
                    { type: "button", className: "library-secondary-button", disabled: isSubmitting,
                      onClick: handleDeleteBook },
                    "删除书籍"
                  ),
                  ReactLib.createElement("span", { className: "library-label" }, "导出仅包含已通过审核的章节")
                )
              : null,
            selectedBook && bookEdit
              ? ReactLib.createElement(
                  "div",
                  { className: "library-form-grid" },
                  ["title", "platform", "genre"].map(function (field) {
                    return ReactLib.createElement(FormField, {
                      key: field, wrapped: true, label: { title: "书名", platform: "平台", genre: "题材" }[field],
                      value: bookEdit[field] || "",
                      onValueChange: function (value) {
                        setBookEdit(function (current) { return Object.assign({}, current, { [field]: value }); });
                      },
                    });
                  }),
                  ReactLib.createElement(
                    "div",
                    { className: "library-button-row" },
                    ReactLib.createElement(
                      "button",
                      { type: "button", onClick: handleSaveBook, disabled: isSubmitting },
                      "保存书籍信息"
                    )
                  )
                )
              : null
          ),
          ReactLib.createElement(
            "section",
            { className: "library-panel" },
            ReactLib.createElement("h2", null, "章节进度"),
            ReactLib.createElement(
              "div",
              { className: "library-button-row" },
              ReactLib.createElement(
                "input",
                {
                  value: chapterSearch,
                  placeholder: "搜索章节标题或章节号",
                  onChange: function (event) {
                    setChapterSearch(event.target.value);
                  },
                }
              ),
              ReactLib.createElement(
                "select",
                {
                  value: chapterStatusFilter,
                  onChange: function (event) {
                    setChapterStatusFilter(event.target.value);
                  },
                },
                ReactLib.createElement("option", { value: "all" }, "全部状态"),
                ReactLib.createElement("option", { value: "draft_planned" }, statusLabel("draft_planned")),
                ReactLib.createElement("option", { value: "review_pending" }, statusLabel("review_pending")),
                ReactLib.createElement("option", { value: "revision_pending" }, statusLabel("revision_pending")),
                ReactLib.createElement("option", { value: "persisted" }, statusLabel("persisted")),
                ReactLib.createElement("option", { value: "publish_pending" }, statusLabel("publish_pending"))
              )
            ),
            chapters.length === 0
              ? ReactLib.createElement("div", { className: "library-empty" }, "当前书籍还没有章节。")
              : ReactLib.createElement(
                  "div",
                  { className: "library-chapter-table" },
                  ReactLib.createElement(
                    "div",
                    { className: "library-table-head" },
                    ReactLib.createElement("span", null, "章节"),
                    ReactLib.createElement("span", null, "标题"),
                    ReactLib.createElement("span", null, "状态"),
                    ReactLib.createElement("span", null, "发布")
                  ),
                  filteredChapters.map(function (chapter) {
                    const latestJob = jobMap[chapter.id];
                    return ReactLib.createElement(
                      "button",
                      {
                        type: "button",
                        className: selectedChapterId === chapter.id ? "library-table-row active" : "library-table-row",
                        key: chapter.id,
                        onClick: function () {
                          setSelectedChapterId(chapter.id);
                        },
                      },
                      ReactLib.createElement("span", null, "#" + chapter.chapter_number),
                      ReactLib.createElement("span", null, chapter.title),
                      ReactLib.createElement(StatusBadge, { className: "library-chip", "data-tone": getStatusTone(chapter.status), status: chapter.status }),
                      ReactLib.createElement(StatusBadge, { className: "library-chip", "data-tone": getStatusTone(latestJob ? latestJob.status : "idle") }, latestJob ? statusLabel(latestJob.status) : "无任务")
                    );
                  })
                )
          ),
          ReactLib.createElement(
            "section",
            { className: "library-panel" },
            ReactLib.createElement("h2", null, "快速跳转"),
            !selectedChapter
              ? ReactLib.createElement("div", { className: "library-empty" }, "先选择一个章节。")
              : ReactLib.createElement(
                  "div",
                  { className: "library-job-panel" },
                  ReactLib.createElement(
                    "div",
                    { className: "library-job-summary" },
                    ReactLib.createElement("strong", null, "当前章节："),
                    ReactLib.createElement("span", null, selectedChapter.title)
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "library-job-summary" },
                    ReactLib.createElement("strong", null, "当前版本："),
                    ReactLib.createElement("span", null, selectedChapter.current_version_id || "暂无已保存版本")
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "library-job-summary" },
                    ReactLib.createElement("strong", null, "已保存版本："),
                    ReactLib.createElement("span", null, selectedChapter.current_version_id ? "已有可回填版本" : "尚未保存过正文")
                  ),
                  selectedChapter.status === "generation_failed"
                    ? ReactLib.createElement(Feedback, {
                        className: "library-job-error",
                        message: "生成失败：" + (selectedChapter.error_message || "原因未记录"),
                      })
                    : null,
                  ReactLib.createElement(
                    "div",
                    { className: "library-button-row" },
                    ReactLib.createElement(
                      "button",
                      { type: "button", className: "library-secondary-button", disabled: isSubmitting,
                        onClick: function () {
                          setChapterEdit(chapterEdit ? null : {
                            title: selectedChapter.title, chapterNumber: String(selectedChapter.chapter_number),
                          });
                        } },
                      chapterEdit ? "取消编辑" : "编辑章节"
                    ),
                    ReactLib.createElement(
                      "button",
                      { type: "button", className: "library-secondary-button", disabled: isSubmitting,
                        onClick: handleDeleteChapter },
                      "删除章节"
                    ),
                    selectedChapter.status === "generation_failed"
                      ? ReactLib.createElement(
                          "button",
                          {
                            type: "button",
                            className: "library-secondary-button",
                            onClick: function () {
                              handleRequeueGeneration(selectedChapter.id);
                            },
                            disabled: isSubmitting,
                          },
                          "重新排队生成"
                        )
                      : null,
                    ReactLib.createElement(
                      "a",
                      {
                        className: "library-action-inline",
                        href: buildCreationLink(selectedBook ? selectedBook.id : "", selectedChapter.id),
                      },
                      "进入创作页"
                    ),
                    ReactLib.createElement(
                      "a",
                      {
                        className: "library-action-inline",
                        href: "/chapters/" + selectedChapter.id + "/detail",
                      },
                      "查看章节详情"
                    )
                  ),
                  chapterEdit
                    ? ReactLib.createElement(
                        "div",
                        { className: "library-form-grid" },
                        ReactLib.createElement(FormField, {
                          label: "章节号", wrapped: true, type: "number", min: 1, value: chapterEdit.chapterNumber,
                          onValueChange: function (value) {
                            setChapterEdit(function (current) { return Object.assign({}, current, { chapterNumber: value }); });
                          },
                        }),
                        ReactLib.createElement(FormField, {
                          label: "标题", wrapped: true, value: chapterEdit.title,
                          onValueChange: function (value) {
                            setChapterEdit(function (current) { return Object.assign({}, current, { title: value }); });
                          },
                        }),
                        ReactLib.createElement(
                          "div",
                          { className: "library-button-row" },
                          ReactLib.createElement(
                            "button",
                            { type: "button", onClick: handleSaveChapter, disabled: isSubmitting },
                            "保存章节信息"
                          )
                        )
                      )
                    : null,
                  selectedChapterJob
                    ? ReactLib.createElement(
                        "div",
                        { className: "library-job-summary" },
                        ReactLib.createElement("strong", null, "发布状态："),
                        ReactLib.createElement(StatusBadge, { className: "library-chip", "data-tone": getStatusTone(selectedChapterJob.status), status: selectedChapterJob.status }),
                        ReactLib.createElement("span", null, selectedChapterJob.created_at || "时间未知（历史记录）")
                      )
                    : null
                )
          ),
          ReactLib.createElement(
            "section",
            { className: "library-panel" },
            ReactLib.createElement("h2", null, "新建章节"),
            ReactLib.createElement(
              "div",
              { className: "library-form-grid" },
              ReactLib.createElement(FormField, {
                label: "章节号", wrapped: true, value: chapterForm.chapterNumber,
                onValueChange: value => updateChapterForm("chapterNumber", value),
              }),
              ReactLib.createElement(FormField, {
                label: "标题", wrapped: true, value: chapterForm.title,
                onValueChange: value => updateChapterForm("title", value),
              }),
              ReactLib.createElement(FormField, {
                label: "章节目标", wrapped: true, labelClassName: "library-form-wide", as: "textarea", value: chapterForm.objective,
                onValueChange: value => updateChapterForm("objective", value),
              })
            ),
            ReactLib.createElement(
              "div",
              { className: "library-button-row" },
              ReactLib.createElement(
                "button",
                { type: "button", onClick: handleCreateChapter, disabled: isSubmitting || !selectedBook },
                "新建章节"
              )
            )
          ),
          ReactLib.createElement(
            "section",
            { className: "library-panel" },
            ReactLib.createElement("h2", null, "发布管理"),
            !selectedChapterId
              ? ReactLib.createElement("div", { className: "library-empty" }, "先选择一个章节。")
              : ReactLib.createElement(
                  "div",
                  { className: "library-job-panel" },
                  ReactLib.createElement(
                    "div",
                    { className: "library-job-summary" },
                    ReactLib.createElement("strong", null, "当前章节："),
                    ReactLib.createElement("span", null, selectedChapter ? selectedChapter.title : selectedChapterId)
                  ),
                  ReactLib.createElement(
                    "div",
                    { className: "library-job-summary" },
                    ReactLib.createElement("strong", null, "最近发布状态："),
                    ReactLib.createElement(StatusBadge, { className: "library-chip", "data-tone": getStatusTone(selectedChapterJob ? selectedChapterJob.status : "idle") }, selectedChapterJob ? statusLabel(selectedChapterJob.status) : "无任务")
                  ),
                  selectedChapterJob && selectedChapterJob.error_message
                    ? ReactLib.createElement(
                        "div",
                        { className: "library-job-error" },
                        selectedChapterJob.error_message
                      )
                    : null,
                  ReactLib.createElement(
                    "div",
                    { className: "library-button-row" },
                    ReactLib.createElement(
                      "button",
                      {
                        type: "button",
                        className: "library-secondary-button",
                        onClick: function () {
                          handleRetryPublish(selectedChapterId);
                        },
                        disabled: isSubmitting || !selectedChapterJob,
                      },
                      "重试发布"
                    ),
                    ReactLib.createElement(
                      "button",
                      {
                        type: "button",
                        className: "library-secondary-button",
                        onClick: function () {
                          handleArchiveFailure(selectedChapterId);
                        },
                        disabled: isSubmitting || !selectedChapterJob,
                      },
                      "归档失败"
                    ),
                    ReactLib.createElement(
                      "a",
                      {
                        className: "library-action-inline",
                        href: buildCreationLink(selectedBook ? selectedBook.id : "", selectedChapterId),
                      },
                      "继续写这一章"
                    )
                  )
                )
          ),
          ReactLib.createElement(
            "section",
            { className: "library-panel" },
            ReactLib.createElement("h2", null, "下一步操作"),
            ReactLib.createElement(
              "div",
              { className: "library-action-grid" },
              ReactLib.createElement(
                "a",
                { className: "library-action-card", href: "/outline" },
                ReactLib.createElement("strong", null, "前往大纲生成"),
                ReactLib.createElement("span", null, "补全三幕结构、章节种子和风格规则。")
              ),
              ReactLib.createElement(
                "a",
                {
                  className: "library-action-card",
                  href: buildCreationLink(selectedBook ? selectedBook.id : "", selectedChapterId),
                },
                ReactLib.createElement("strong", null, "进入创作驾驶舱"),
                ReactLib.createElement("span", null, "根据当前书籍状态继续生产下一章。")
              )
            )
          )
        )
      )
    );
  }

  const rootElement = document.getElementById("library-root");
  ReactDOMLib.createRoot(rootElement).render(ReactLib.createElement(App));
})();
