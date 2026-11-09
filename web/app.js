(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var sessionId = null;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  // ---------- Upload ----------
  function setStatus(text, isError) {
    var s = $("upload-status");
    s.textContent = text;
    s.className = "status" + (isError ? " error" : "");
  }

  async function upload(file) {
    if (!file) return;
    setStatus("Enviando e carregando no DuckDB…");
    var body = new FormData();
    body.append("file", file);
    try {
      var res = await fetch("/upload", { method: "POST", body: body });
      var data = await res.json();
      if (!res.ok) throw new Error(data.error || "Erro " + res.status);
      start(data);
    } catch (err) {
      setStatus(err.message || "Falha no envio.", true);
    }
  }

  function start(data) {
    sessionId = data.session_id;
    $("upload-box").hidden = true;
    $("workspace").hidden = false;
    $("filename").textContent = data.filename;
    $("report").href = "/sessions/" + sessionId + "/report.md";
    renderTables(data.tables);
    renderExamples(data.tables);
    $("log").textContent = "";
    $("question").focus();
  }

  function renderTables(tables) {
    var box = $("tables");
    box.textContent = "";
    tables.forEach(function (t) {
      var d = el("details");
      d.appendChild(el("summary", null, t.name + " · " + t.rows.toLocaleString("pt-BR") + " linhas · " + t.columns.length + " colunas"));
      var ul = el("ul");
      t.columns.forEach(function (c) {
        var li = el("li");
        li.appendChild(document.createTextNode(c.name + " "));
        li.appendChild(el("code", null, c.type));
        ul.appendChild(li);
      });
      d.appendChild(ul);
      box.appendChild(d);
    });
  }

  function renderExamples(tables) {
    var box = $("examples");
    box.textContent = "";
    var first = tables[0] && tables[0].name;
    ["Faça um resumo geral desta planilha", "Quais colunas têm dados faltando ou inconsistentes?",
     first ? "Mostre um gráfico com os principais números de " + first : "Mostre um gráfico dos principais números"]
      .forEach(function (q) {
        var b = el("button", null, q);
        b.type = "button";
        b.addEventListener("click", function () { $("question").value = q; $("form").requestSubmit(); });
        var li = el("li");
        li.appendChild(b);
        box.appendChild(li);
      });
  }

  // ---------- Chat ----------
  function renderChart(container, spec) {
    var box = el("div", "chart-box");
    var canvas = el("canvas");
    box.appendChild(canvas);
    container.appendChild(box);
    if (!window.Chart) {
      box.textContent = "Gráfico indisponível (biblioteca não carregou).";
      return;
    }
    var palette = ["#2874a6", "#e67e22", "#27ae60", "#8e44ad", "#c0392b", "#16a085"];
    var type = spec.kind === "scatter" ? "scatter" : spec.kind;
    var datasets = spec.datasets.map(function (d, i) {
      var color = palette[i % palette.length];
      var isPie = spec.kind === "pie";
      return {
        label: d.label, data: d.data,
        backgroundColor: isPie ? d.data.map(function (_, j) { return palette[j % palette.length]; }) : color,
        borderColor: isPie ? undefined : color,
      };
    });
    var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    window.Chart.defaults.color = dark ? "#e6ebf1" : "#1c2430";
    new window.Chart(canvas, {
      type: type,
      data: { labels: spec.labels, datasets: datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { title: { display: !!spec.title, text: spec.title } },
      },
    });
  }

  async function ask(question) {
    var log = $("log");
    log.appendChild(el("div", "msg user", question));
    var bot = el("div", "msg bot");
    var steps = el("div", "steps");
    var text = el("span", null, "…");
    bot.appendChild(steps);
    bot.appendChild(text);
    log.appendChild(bot);
    var started = false;
    $("send").disabled = true;

    function scroll() { bot.scrollIntoView({ block: "end" }); }

    try {
      var res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: question }),
      });
      if (!res.ok) {
        var detail = "";
        try { detail = (await res.json()).error; } catch (e) { /* corpo não-JSON */ }
        throw new Error(detail || "Erro " + res.status);
      }
      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      for (;;) {
        var r = await reader.read();
        if (r.done) break;
        buffer += decoder.decode(r.value, { stream: true });
        var blocks = buffer.split("\n\n");
        buffer = blocks.pop();
        blocks.forEach(function (block) {
          var m = /^event: (\w+)\ndata: (.*)$/s.exec(block);
          if (!m) return;
          var data = JSON.parse(m[2]);
          if (m[1] === "delta") {
            if (!started) { text.textContent = ""; started = true; }
            text.textContent += data.text;
          } else if (m[1] === "tool") {
            var label = data.name === "run_sql" ? "SQL" : data.name === "make_chart" ? "gráfico" : "perfil da tabela";
            var chip = el("span", "step", label);
            if (data.name === "run_sql" && data.input && data.input.query) chip.title = data.input.query;
            steps.appendChild(chip);
          } else if (m[1] === "chart") {
            renderChart(bot, data);
          } else if (m[1] === "warning") {
            bot.appendChild(el("div", "warning", "Atenção: " + data.unverified.join(", ") +
              " não aparece(m) nos resultados das consultas desta resposta. Confira antes de usar."));
          } else if (m[1] === "error") {
            bot.className = "msg error";
            if (!started) text.textContent = "";
            text.textContent += (text.textContent ? "\n\n" : "") + data.error;
          }
          scroll();
        });
      }
    } catch (err) {
      bot.className = "msg error";
      text.textContent = err.message || "Não foi possível falar com o agente.";
    } finally {
      $("send").disabled = false;
      $("question").focus();
    }
  }

  // ---------- Eventos ----------
  $("file").addEventListener("change", function (e) { upload(e.target.files[0]); });
  var drop = $("drop");
  ["dragenter", "dragover"].forEach(function (n) {
    drop.addEventListener(n, function (e) { e.preventDefault(); drop.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (n) {
    drop.addEventListener(n, function (e) { e.preventDefault(); drop.classList.remove("over"); });
  });
  drop.addEventListener("drop", function (e) { upload(e.dataTransfer.files[0]); });

  $("form").addEventListener("submit", function (e) {
    e.preventDefault();
    var q = $("question").value.trim();
    if (!q) return;
    $("question").value = "";
    ask(q);
  });

  $("reset").addEventListener("click", async function () {
    if (sessionId) { try { await fetch("/sessions/" + sessionId, { method: "DELETE" }); } catch (e) { /* ignora */ } }
    sessionId = null;
    $("workspace").hidden = true;
    $("upload-box").hidden = false;
    $("file").value = "";
    setStatus("");
  });
})();
