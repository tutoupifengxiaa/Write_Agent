(function () {
  const ReactLib = window.React;

  async function requestJson(url, options) {
    const response = await fetch(url, options);
    if (response.status === 204) {
      return null;
    }
    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      throw new Error(response.ok ? "服务返回的数据无法读取，请重试。" : "请求失败（" + response.status + "），请重试。");
    }
    if (!response.ok) {
      const detail = payload && payload.detail;
      const message = typeof detail === "string" ? detail : Array.isArray(detail)
        ? detail.map(function (item) { return item.msg || "输入内容有误"; }).join("；")
        : "请求失败（" + response.status + "），请重试。";
      throw new Error(message);
    }
    return payload;
  }

  function fetchJson(url, options) {
    return requestJson(url, options);
  }

  function NavLink(props) {
    return ReactLib.createElement("a", {
      className: props.primary ? "app-link primary" : "app-link",
      href: props.href,
      "aria-current": props.primary ? "page" : undefined,
    }, props.label);
  }

  function Navigation(props) {
    const links = [
      ["/hub", "统一入口"], ["/radar", "选题雷达"], ["/outline", "大纲生成"],
      ["/creation", "创作台"], ["/review-center", "人工审核"],
      ["/library", "文章管理"], ["/ops", "运营看板"],
    ];
    return ReactLib.createElement("nav", { className: "app-nav", "aria-label": "工作区导航" },
      links.map(function (link) {
        return ReactLib.createElement(NavLink, { key: link[0], href: link[0], label: link[1], primary: props.current === link[0] });
      })
    );
  }

  function statusLabel(value) {
    const labels = {
      planned: "已规划", draft_planned: "待写作", context_ready: "上下文就绪", persisted: "已保存", processed: "已处理", attention_required: "需要处理", busy: "忙碌", draft_uploaded: "草稿已上传", pending: "待处理", draft: "草稿", writing: "写作中", generating: "生成中",
      running: "运行中", queued: "排队中", review_pending: "待审核", revision_pending: "待修订",
      approved: "已通过", completed: "已完成", succeeded: "已成功", success: "成功",
      publish_pending: "待发布", publishing: "发布中", published: "已发布", published_draft: "已存平台草稿",
      failed: "失败", publish_failed: "发布失败", archived: "已归档", cancelled: "已取消",
      paused: "已暂停", blocked: "已阻塞", skipped: "已跳过", stopped: "已停止", idle: "空闲",
      ok: "正常", partial: "部分成功", error: "出错", unavailable: "暂不可用", empty: "暂无数据",
    };
    return labels[value] || value || "暂无状态";
  }

  window.WriteAgentUI = { fetchJson: fetchJson, requestJson: requestJson, NavLink: NavLink, Navigation: Navigation, statusLabel: statusLabel };
})();
