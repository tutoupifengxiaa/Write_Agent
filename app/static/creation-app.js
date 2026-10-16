(function () {
  const {createElement: h, useEffect, useRef, useState} = window.React;
  const {fetchJson, requestJson, Navigation, statusLabel} = window.WriteAgentUI;
  const {parseSseChunk, applyJsonSnapshot, looksLikeJsonChunk} = window.CreationStream;
  const {renderRetrievalAudit} = window.CreationPanels;
  const EMPTY_FORM = {chapterTitle: "", chapterGoal: "", mustInclude: "", mustAvoid: "", styleRules: "", targetWords: 2500};
  const normalizeLines = value => Array.isArray(value) ? value.join("\n") : String(value || "");
  const snapshot = (title, content, summary) => JSON.stringify([title, content, summary]);

  function Field({id, label, value, onChange, disabled, type}) {
    return h("label", {className: "task-field", htmlFor: id}, label,
      h(type ? "input" : "textarea", {id, type, value, disabled, min: type === "number" ? 1 : undefined,
        max: type === "number" ? 50000 : undefined, onChange: event => onChange(event.target.value)}));
  }

  function App() {
    const [form, setForm] = useState(EMPTY_FORM);
    const [draft, setDraft] = useState("");
    const [summary, setSummary] = useState("");
    const [stateDeltas, setStateDeltas] = useState([]);
    const [chapter, setChapter] = useState(null);
    const [bookTitle, setBookTitle] = useState("尚未选择书籍");
    const [versionHistory, setVersionHistory] = useState([]);
    const [selectedVersionId, setSelectedVersionId] = useState("");
    const [manualReviewNote, setManualReviewNote] = useState("");
    const [feedback, setFeedback] = useState({text: "", error: false});
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState("");
    const [preview, setPreview] = useState(null);
    const [savedSnapshot, setSavedSnapshot] = useState(snapshot("", "", ""));
    const controllerRef = useRef(null);
    const dirty = snapshot(form.chapterTitle, draft, summary) !== savedSnapshot;
    const dirtyRef = useRef(false);
    dirtyRef.current = dirty || busy === "generate";
    const selectedVersion = versionHistory.find(item => item.id === selectedVersionId);
    const notify = (text, error = false) => setFeedback({text, error});
    const updateField = (field, value) => setForm(current => ({...current, [field]: value}));
    const canPersist = !!chapter && !!form.chapterTitle.trim() && !!draft.trim() && !busy && !loading;

    async function loadChapter() {
      setLoading(true);
      const params = new URLSearchParams(window.location.search);
      const chapterId = params.get("chapter_id");
      if (!chapterId) {
        notify("从文章管理选择章节后，可保存正文和提交审核。");
        setLoading(false);
        return;
      }
      try {
        const [detail, versions] = await Promise.all([
          fetchJson("/chapters/" + encodeURIComponent(chapterId)),
          fetchJson("/chapters/" + encodeURIComponent(chapterId) + "/versions")
        ]);
        const book = await fetchJson("/books/" + encodeURIComponent(detail.book_id));
        const goal = detail.goal || {};
        const rules = goal.style_rules || book.style_rules || "";
        setChapter(detail);
        setBookTitle(book.title);
        setForm({chapterTitle: detail.title || "", chapterGoal: goal.objective || "",
          mustInclude: normalizeLines(goal.must_include), mustAvoid: normalizeLines(goal.must_avoid),
          styleRules: typeof rules === "object" ? Object.entries(rules).map(([key, value]) => key + ": " + value).join("\n") : rules,
          targetWords: goal.target_words || 2500});
        setManualReviewNote(goal.manual_review_note || "");
        setVersionHistory(versions);
        const current = versions[0] || {};
        setSelectedVersionId(current.id || "");
        setDraft(current.content || "");
        setSummary(current.summary || "");
        setStateDeltas(current.state_deltas || []);
        setSavedSnapshot(snapshot(detail.title || "", current.content || "", current.summary || ""));
        notify("");
      } catch (error) {
        notify("章节加载失败：" + error.message, true);
      } finally {
        setLoading(false);
      }
    }

    useEffect(() => {
      loadChapter();
      const beforeUnload = event => {
        if (dirtyRef.current) { event.preventDefault(); event.returnValue = ""; }
      };
      const beforeNavigate = event => {
        const link = event.target.closest("a[href]");
        if (link && dirtyRef.current && !window.confirm("有未保存的更改，确定离开当前章节？")) event.preventDefault();
      };
      window.addEventListener("beforeunload", beforeUnload);
      document.addEventListener("click", beforeNavigate, true);
      document.getElementById("creation-root").dataset.ready = "true";
      return () => {
        controllerRef.current?.abort();
        window.removeEventListener("beforeunload", beforeUnload);
        document.removeEventListener("click", beforeNavigate, true);
      };
    }, []);

    async function refreshVersions() {
      const versions = await fetchJson("/chapters/" + encodeURIComponent(chapter.id) + "/versions");
      setVersionHistory(versions);
      setSelectedVersionId(versions[0]?.id || "");
    }

    async function persist(reviewAction, createPublishJob = false) {
      if (!canPersist) return;
      setBusy(reviewAction ? "review" : "save");
      notify("");
      const content = {chapter_id: chapter.id, title: form.chapterTitle, content: draft, summary, state_deltas: stateDeltas};
      try {
        const payload = await requestJson(reviewAction ? "/api/creation/review" : "/api/creation/save", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify(reviewAction ? {...content, review_action: reviewAction, note: manualReviewNote} : {...content, create_publish_job: createPublishJob})
        });
        setSavedSnapshot(snapshot(form.chapterTitle, draft, summary));
        setChapter(current => ({...current, title: form.chapterTitle, status: payload.status}));
        if (!reviewAction) setStateDeltas([]);
        notify(reviewAction ? "已保存当前正文并提交审核" : payload.publish_job_id ? "已保存并加入草稿发布队列" : "已保存入库");
        try { await refreshVersions(); }
        catch (error) { notify("正文已保存，版本列表刷新失败：" + error.message, true); }
      } catch (error) {
        notify("操作失败：" + error.message, true);
      } finally {
        setBusy("");
      }
    }

    async function handleGenerate() {
      if (busy || loading) return;
      if (!form.chapterTitle.trim() || !form.chapterGoal.trim() || !Number.isInteger(Number(form.targetWords)) || Number(form.targetWords) <= 0 || Number(form.targetWords) > 50000) {
        notify("请填写章节标题、目标和有效目标字数（1—50000）。", true);
        return;
      }
      const controller = new AbortController();
      controllerRef.current = controller;
      setBusy("generate");
      let generated = {draft: "", chapterTitle: form.chapterTitle, summary: "", stateDeltas: []};
      let rawJson = "", jsonMode = false, completed = false, reader;
      setPreview(generated);
      notify("正在生成，原稿已保留。可随时停止。");
      const applyEvent = (event, payload) => {
        if (controller.signal.aborted) return;
        if (event === "error") throw new Error(payload.detail || "生成失败");
        if (event === "title") generated.chapterTitle = payload.value || generated.chapterTitle;
        if (event === "summary" && (!jsonMode || payload.value)) generated.summary = payload.value || "";
        if (event === "delta") {
          const value = payload.value || "";
          if (jsonMode || (!generated.draft && looksLikeJsonChunk(value))) {
            jsonMode = true; rawJson += value;
            generated = applyJsonSnapshot(rawJson, generated);
          } else generated.draft += value;
        }
        if (event === "done") {
          completed = payload.ok !== false;
          if (Array.isArray(payload.state_deltas) && (!jsonMode || payload.state_deltas.length)) generated.stateDeltas = payload.state_deltas;
        }
        setPreview({...generated});
      };
      try {
        const response = await fetch("/api/creation/generate-stream", {
          method: "POST", headers: {"Content-Type": "application/json"}, signal: controller.signal,
          body: JSON.stringify({chapter_title: form.chapterTitle, chapter_goal: form.chapterGoal,
            must_include: form.mustInclude, must_avoid: form.mustAvoid, style_rules: form.styleRules, target_words: Number(form.targetWords)})
        });
        if (!response.ok) {
          let message = "生成服务暂不可用（" + response.status + "）";
          try { const data = await response.json(); if (typeof data.detail === "string") message = data.detail; } catch (_) {}
          throw new Error(message);
        }
        if (!response.body) throw new Error("流式响应不可用");
        reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const result = await reader.read();
          if (result.done) break;
          buffer += decoder.decode(result.value, {stream: true});
          buffer = parseSseChunk(buffer, applyEvent);
        }
        buffer += decoder.decode();
        if (buffer.trim()) parseSseChunk(buffer + "\n\n", applyEvent);
        if (controller.signal.aborted) throw new DOMException("Stopped", "AbortError");
        if (jsonMode) {
          let result;
          try { result = JSON.parse(rawJson); }
          catch (_) { throw new Error("生成的 JSON 未完整结束，请重试"); }
          if (!result || typeof result.content !== "string") throw new Error("生成结果缺少正文");
          generated = applyJsonSnapshot(rawJson, generated);
        }
        if (!completed || !generated.draft.trim()) throw new Error("生成未完整结束，请重试");
        setDraft(generated.draft);
        setSummary(generated.summary);
        setStateDeltas(generated.stateDeltas);
        updateField("chapterTitle", generated.chapterTitle);
        notify("生成完成，请检查正文后保存。");
      } catch (error) {
        notify(error.name === "AbortError" ? "已停止生成，原稿未改变。" : "生成失败，原稿已保留：" + error.message, error.name !== "AbortError");
      } finally {
        if (reader) { try { await reader.cancel(); } catch (_) {} reader.releaseLock(); }
        controllerRef.current = null;
        setPreview(null);
        setBusy("");
      }
    }

    function applyVersionToDraft(version) {
      if (busy || (dirty && !window.confirm("用选中版本替换未保存的正文？"))) return;
      setSelectedVersionId(version.id);
      setDraft(version.content || "");
      setSummary(version.summary || "");
      setStateDeltas(version.state_deltas || []);
      notify("已回填选中版本，保存后生效。");
    }

    const field = (id, label, key, type) => h(Field, {id, label, type, value: form[key], disabled: !!busy || loading, onChange: value => updateField(key, value)});
    return h("main", {className: "shell"},
      h("header", {className: "hero"},
        h("div", {className: "heading-row"}, h("div", null, h("p", {className: "eyebrow"}, "WRITE AGENT / WORKSPACE"), h("h1", null, "创作台")),
          h("span", {className: "save-status", "data-dirty": dirty}, busy ? "处理中…" : dirty ? "有未保存更改" : "已同步")),
        h(Navigation, {current: "/creation"}),
        h("div", {className: "chapter-context"}, h("strong", null, bookTitle),
          chapter ? h("span", null, "第 " + chapter.chapter_number + " 章 · " + chapter.title) : h("a", {href: "/library"}, "选择章节"),
          chapter ? h("span", {className: "state-chip"}, statusLabel(chapter.status)) : null)),
      feedback.text ? h("div", {id: "feedback-output", className: "feedback", role: feedback.error ? "alert" : "status", "data-error": feedback.error},
        feedback.text, feedback.error && !chapter ? h("button", {className: "secondary", disabled: loading, onClick: loadChapter}, "重新加载") : null) : null,
      loading ? h("p", {role: "status"}, "正在加载章节…") : null,
      h("div", {className: "workspace"},
        h("section", {className: "panel editor-panel"},
          h("div", {className: "panel-heading"}, h("h2", null, "正文"), h("span", {id: "word-count-output"}, (preview ? preview.draft.length : draft.length) + " / " + form.targetWords + " 字")),
          preview ? h("p", {className: "preview-note", role: "status"}, "生成预览 · 完整结束后替换原稿") : null,
          h("textarea", {id: "draft-output", className: "draft", "aria-label": "章节正文", value: preview ? preview.draft : draft,
            disabled: !!busy || loading, placeholder: "开始写作，或填写任务后生成正文…", onChange: event => setDraft(event.target.value)}),
          h(Field, {id: "chapter-summary", label: "章节摘要", value: summary, disabled: !!busy || loading, onChange: setSummary}),
          h("span", {id: "summary-output", className: "visually-hidden"}, summary)),
        h("aside", {className: "support-panels"},
          h("details", {className: "panel", open: !chapter}, h("summary", null, "章节任务与约束"),
            field("chapter-title", "章节标题", "chapterTitle", "text"), field("chapter-goal", "章节目标", "chapterGoal"),
            field("target-words", "目标字数", "targetWords", "number"), field("must-include", "必须包含", "mustInclude"),
            field("must-avoid", "必须避免", "mustAvoid"), field("style-rules", "风格规则", "styleRules")),
          h("details", {className: "panel"}, h("summary", null, "版本历史 · " + versionHistory.length),
            !versionHistory.length ? h("p", null, "暂无已保存版本") : versionHistory.map(version => h("article", {className: "version-card", key: version.id},
              h("strong", null, version.summary || "未填写摘要"), h("small", null, version.created_at || "时间未知"),
              h("div", {className: "button-row"}, h("button", {className: "secondary", onClick: () => setSelectedVersionId(version.id)}, "预览版本"),
                h("button", {className: "secondary", disabled: !!busy, onClick: () => applyVersionToDraft(version)}, "回填到编辑区")))),
            selectedVersion ? h("div", {className: "version-preview"}, h("h3", null, "版本预览"), h("p", null, selectedVersion.content || "该版本没有正文")) : null),
          h("details", {className: "panel"}, h("summary", null, "审核与检索证据"),
            h(Field, {id: "manual-review-note", label: "审核备注", value: manualReviewNote, disabled: !!busy, onChange: setManualReviewNote}),
            selectedVersion ? renderRetrievalAudit(selectedVersion) : h("p", null, "暂无检索证据"),
            h("div", {id: "state-deltas-output"}, h("h3", null, "待审核状态变化"),
              stateDeltas.length ? stateDeltas.map((item, index) => h("p", {key: index}, item.change || JSON.stringify(item))) : h("p", null, "暂无待审核状态变化")),
            h("button", {className: "secondary", disabled: !canPersist, onClick: () => persist("revision_pending")}, "保存并标记待修订"),
            h("a", {href: "/review-center"}, "前往人工审核台")),
          h("details", {className: "panel"}, h("summary", null, "草稿发布"), h("p", null, "保存当前正文并加入番茄草稿队列，发布进度可在文章管理查看。"),
            h("button", {className: "secondary", disabled: !canPersist, onClick: () => persist(null, true)}, "推送番茄草稿")))),
      h("footer", {className: "actions", "aria-label": "章节操作"},
        busy === "generate" ? h("button", {id: "stop-generation-button", className: "secondary", onClick: () => controllerRef.current?.abort()}, "停止生成") :
          h("button", {id: "generate-chapter-button", className: "secondary", disabled: !!busy || loading, onClick: handleGenerate}, "生成章节"),
        h("button", {disabled: !canPersist, onClick: () => persist(null)}, busy === "save" ? "保存中…" : "保存入库"),
        h("button", {className: "secondary", disabled: !canPersist, onClick: () => persist("review_pending")}, busy === "review" ? "提交中…" : "保存并提交审核")));
  }
  window.ReactDOM.createRoot(document.getElementById("creation-root")).render(h(App));
})();
