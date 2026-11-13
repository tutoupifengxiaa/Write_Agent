(function () {
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


  window.CreationStream = {parseSseChunk};
})();
