(function () {
  const ReactLib = window.React;
  function renderRetrievalAudit(version) {
    const generationMetadata = version && version.generation_metadata ? version.generation_metadata : {};
    const retrievalEvidence = version && version.generation_metadata && Array.isArray(version.generation_metadata.retrieval_evidence)
      ? version.generation_metadata.retrieval_evidence
      : [];
    const retrievalMode = generationMetadata.retrieval_mode || "none";
    const degradationReason = generationMetadata.retrieval_degradation_reason || "";

    return ReactLib.createElement(
      "div",
      { className: "delta-card", "data-audit-section": "retrieval-evidence" },
      ReactLib.createElement("strong", null, "检索证据"),
      ReactLib.createElement(
        "div",
        { className: "meta-row" },
        ReactLib.createElement("span", { className: "pill" }, "模式：" + retrievalMode),
        degradationReason
          ? ReactLib.createElement("span", { className: "pill" }, "降级原因：" + degradationReason)
          : null
      ),
      retrievalEvidence.length === 0
        ? ReactLib.createElement("div", null, "暂无检索证据")
        : retrievalEvidence.map(function (evidence, index) {
            return ReactLib.createElement(
              "div",
              { className: "delta-card", key: evidence.source_id || index },
              ReactLib.createElement("strong", null, evidence.source_type || "unknown"),
              ReactLib.createElement("div", null, evidence.excerpt || "未记录摘录"),
              ReactLib.createElement("div", null, "分数：" + String(evidence.score === undefined ? "unknown" : evidence.score)),
              ReactLib.createElement("div", null, "入选原因：" + (evidence.selection_reason || "未记录"))
            );
          })
    );
  }


  window.CreationPanels = {renderRetrievalAudit};
})();
