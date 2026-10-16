(function () {
  function safeParseJson(value) {
    try {
      return JSON.parse(value);
    } catch (_error) {
      return null;
    }
  }

  function decodePartialJsonString(value) {
    return value
      .replace(/\\\\/g, "\\u005c")
      .replace(/\\"/g, "\"")
      .replace(/\\n/g, "\n")
      .replace(/\\r/g, "\r")
      .replace(/\\t/g, "\t")
      .replace(/\\u([0-9a-fA-F]{4})/g, function (_match, code) {
        return String.fromCharCode(parseInt(code, 16));
      })
      .replace(/\\u005c/g, "\\");
  }

  function looksLikeJsonChunk(value) {
    const trimmed = (value || "").trimStart();
    return (
      trimmed.startsWith("{") ||
      trimmed.startsWith('"title"') ||
      trimmed.startsWith('"content"') ||
      trimmed.startsWith('"summary"') ||
      trimmed.startsWith('"state_deltas"') ||
      trimmed.startsWith('title"') ||
      trimmed.startsWith('content"') ||
      trimmed.startsWith('summary"') ||
      trimmed.startsWith('state_deltas"') ||
      trimmed.startsWith("[") ||
      trimmed.startsWith("]") ||
      trimmed.startsWith("}")
    );
  }

  function extractPartialStringField(rawText, fieldName) {
    const match = new RegExp('"' + fieldName + '"\\s*:\\s*"').exec(rawText);
    const start = match ? match.index : -1;
    if (start === -1) {
      return "";
    }

    let cursor = start + match[0].length;
    let escaped = false;
    let result = "";

    while (cursor < rawText.length) {
      const char = rawText[cursor];
      if (escaped) {
        result += `\\${char}`;
        escaped = false;
      } else if (char === "\\") {
        escaped = true;
      } else if (char === "\"") {
        break;
      } else {
        result += char;
      }
      cursor += 1;
    }

    return decodePartialJsonString(result);
  }

  function extractPartialStateDeltas(rawText) {
    const marker = '"state_deltas":';
    const start = rawText.indexOf(marker);
    if (start === -1) {
      return [];
    }

    const arrayStart = rawText.indexOf("[", start);
    if (arrayStart === -1) {
      return [];
    }

    let depth = 0;
    let end = -1;
    for (let index = arrayStart; index < rawText.length; index += 1) {
      const char = rawText[index];
      if (char === "[") depth += 1;
      if (char === "]") {
        depth -= 1;
        if (depth === 0) {
          end = index + 1;
          break;
        }
      }
    }

    if (end === -1) {
      return [];
    }

    const candidate = rawText.slice(arrayStart, end);
    const parsed = safeParseJson(candidate);
    return Array.isArray(parsed) ? parsed : [];
  }

  function applyJsonSnapshot(rawJson, previousState) {
    const parsed = safeParseJson(rawJson);
    if (parsed) {
      return {
        draft: typeof parsed.content === "string" ? parsed.content : previousState.draft,
        chapterTitle:
          typeof parsed.title === "string" && parsed.title.trim()
            ? parsed.title
            : previousState.chapterTitle,
        summary:
          typeof parsed.summary === "string"
            ? parsed.summary
            : previousState.summary,
        stateDeltas:
          Array.isArray(parsed.state_deltas)
            ? parsed.state_deltas
            : previousState.stateDeltas,
      };
    }

    const partialDraft = extractPartialStringField(rawJson, "content");
    const partialTitle = extractPartialStringField(rawJson, "title");
    const partialSummary = extractPartialStringField(rawJson, "summary");
    const partialStateDeltas = extractPartialStateDeltas(rawJson);

    return {
      draft: partialDraft || previousState.draft,
      chapterTitle: partialTitle || previousState.chapterTitle,
      summary: partialSummary || previousState.summary,
      stateDeltas: partialStateDeltas.length > 0 ? partialStateDeltas : previousState.stateDeltas,
    };
  }

  function parseSseChunk(buffer, onEvent) {
    const records = buffer.replace(/\r\n/g, "\n").split("\n\n");
    const remainder = records.pop() || "";

    records.forEach(function (record) {
      if (!record.trim()) {
        return;
      }

      const lines = record.split("\n");
      const eventLine = lines.find(function (line) {
        return line.startsWith("event:");
      });
      const dataLines = lines.filter(function (line) {
        return line.startsWith("data:");
      });

      if (!eventLine || dataLines.length === 0) {
        return;
      }

      const event = eventLine.slice(6).trim();
      const payloadText = dataLines
        .map(function (line) {
          return line.slice(5).trim();
        })
        .join("\n");

      onEvent(event, JSON.parse(payloadText));
    });

    return remainder;
  }


  window.CreationStream = {parseSseChunk, applyJsonSnapshot, looksLikeJsonChunk};
})();
