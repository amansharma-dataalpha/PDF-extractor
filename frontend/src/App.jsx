import { Component, useEffect, useRef, useState } from "react";
import { deleteDoc, exportUrl, extract, listDocs, retryDoc, uploadDocs } from "./api.js";

const FORMATS = [
  ["table", "Table"],
  ["paragraph", "Paragraph"],
  ["bullets", "Bullet points"],
  ["keyvalue", "Field: Value"],
  ["json", "JSON"],
  ["custom", "Custom…"],
];

// Document lifecycle: processing -> ready_text (finx extraction, extract works) -> ready (Docling check done).
// Scanned PDFs skip ready_text (they wait for OCR). A Docling failure after ready_text keeps the finx result plus an error.
const isUsable = (d) => d.status === "ready" || d.status === "ready_text";
const isParsing = (d) => d.status === "processing" || (d.status === "ready_text" && !d.error);

function statusText(d) {
  if (d.status === "processing") return "Reading PDF…";
  const pages = `${d.pages} page${d.pages === 1 ? "" : "s"}`;
  const checked = d.statements ? ` · ${d.statements} statements, ${d.certified_pct}% of values checked` : "";
  if (d.status === "ready_text") {
    return d.error
      ? `${pages} · ${d.tables} tables. ${d.error}`
      : `${pages} · ${d.tables} tables · second check running…`;
  }
  if (d.status === "ready") return `${pages} · ${d.tables} tables${checked}`;
  return `Error: ${d.error}`;
}

let nextId = 1;
const newCard = () => ({
  id: nextId++,
  instruction: "",
  docIds: [],
  format: "table",
  custom: "",
  loading: false,
  result: null,
  error: "",
});

const cellText = (v) => (v !== null && typeof v === "object" ? JSON.stringify(v) : String(v ?? ""));

// The model is asked for rows as arrays, but sometimes returns objects keyed by column name.
function toRow(columns, r) {
  if (Array.isArray(r)) return r;
  if (r !== null && typeof r === "object") return columns.map((c) => r[c]);
  return [r];
}

function toCsv(columns, rows) {
  const esc = (v) => `"${cellText(v).replace(/"/g, '""')}"`;
  return [columns, ...rows].map((r) => r.map(esc).join(",")).join("\n");
}

function download(name, text, type) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
}

// Keeps a malformed result from blanking the whole page; resets when a new result arrives.
class ResultBoundary extends Component {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidUpdate(prev) {
    if (prev.result !== this.props.result && this.state.failed) this.setState({ failed: false });
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="result">
        <div className="warn">This result could not be displayed. Raw answer:</div>
        <pre className="text">{this.props.result?.text}</pre>
      </div>
    );
  }
}

function Result({ result }) {
  const d = result.data;
  const isTable = result.format === "table" && d && Array.isArray(d.columns) && Array.isArray(d.rows);
  const rows = isTable ? d.rows.map((r) => toRow(d.columns, r)) : [];
  const copyText = isTable ? toCsv(d.columns, rows) : result.text;

  return (
    <div className="result">
      {result.truncated && (
        <div className="warn">Documents were too long and were truncated before extraction.</div>
      )}
      {result.warning && <div className="warn">{result.warning}</div>}
      {isTable ? (
        <div className="tablewrap">
          <table>
            <thead>
              <tr>{d.columns.map((c, i) => <th key={i}>{cellText(c)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>{r.map((c, j) => <td key={j}>{cellText(c)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <pre className="text">{result.text}</pre>
      )}
      <div className="actions">
        <button onClick={() => navigator.clipboard.writeText(copyText)}>Copy</button>
        {isTable && (
          <button onClick={() => download("extract.csv", toCsv(d.columns, rows), "text/csv")}>
            Download CSV
          </button>
        )}
        {result.format === "json" && (
          <button onClick={() => download("extract.json", result.text, "application/json")}>
            Download JSON
          </button>
        )}
        {!isTable && result.format !== "json" && (
          <button onClick={() => download("extract.md", result.text, "text/markdown")}>
            Download .md
          </button>
        )}
      </div>
    </div>
  );
}

export default function App() {
  const [docs, setDocs] = useState([]);
  const [cards, setCards] = useState([newCard()]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const fileRef = useRef();

  const refresh = async () => {
    try {
      setDocs(await listDocs());
    } catch (e) {
      setError(`Cannot reach backend: ${e.message}`);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  // Poll while any document is still being parsed (quick text or Docling).
  useEffect(() => {
    if (!docs.some(isParsing)) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [docs]);

  const onFiles = async (fileList) => {
    const files = [...fileList].filter((f) => f.name.toLowerCase().endsWith(".pdf"));
    if (!files.length) return;
    setUploading(true);
    setError("");
    try {
      const added = (await uploadDocs(files)).map((d) => d.id);
      // Newly uploaded PDFs are selected in every request card right away.
      setCards((cs) => cs.map((c) => ({ ...c, docIds: [...c.docIds, ...added] })));
      await refresh();
    } catch (e) {
      setError(e.message);
    }
    setUploading(false);
  };

  const patch = (id, p) => setCards((cs) => cs.map((c) => (c.id === id ? { ...c, ...p } : c)));

  const toggleDoc = (card, docId) =>
    patch(card.id, {
      docIds: card.docIds.includes(docId)
        ? card.docIds.filter((x) => x !== docId)
        : [...card.docIds, docId],
    });

  const run = async (card) => {
    if (!card.instruction.trim()) return patch(card.id, { error: "Describe what to extract." });
    const selected = docs.filter((d) => card.docIds.includes(d.id));
    if (!selected.length) return patch(card.id, { error: "Select at least one PDF." });
    const notUsable = selected.filter((d) => !isUsable(d));
    if (notUsable.length) {
      const names = notUsable.map((d) => d.name).join(", ");
      return patch(card.id, { error: `Wait until ${names} finishes reading, or untick it.` });
    }
    patch(card.id, { loading: true, error: "", result: null });
    try {
      const result = await extract({
        doc_ids: selected.map((d) => d.id),
        instruction: card.instruction,
        format: card.format === "custom" ? "custom" : card.format,
        custom_format: card.custom,
      });
      patch(card.id, { loading: false, result });
    } catch (e) {
      patch(card.id, { loading: false, error: e.message });
    }
  };

  const runAll = async () => {
    for (const c of cards) await run(c);
  };

  const remove = async (id) => {
    setError("");
    try {
      await deleteDoc(id);
      setCards((cs) => cs.map((c) => ({ ...c, docIds: c.docIds.filter((x) => x !== id) })));
    } catch (e) {
      setError(`Could not delete: ${e.message}`);
    }
    refresh();
  };

  const retry = async (id) => {
    setError("");
    try {
      await retryDoc(id);
    } catch (e) {
      setError(`Could not retry: ${e.message}`);
    }
    refresh();
  };

  // Parsing PDFs are listed too, so a fresh upload shows up (and is ticked) immediately.
  const choosable = docs.filter((d) => d.status !== "error");

  return (
    <div className="app">
      <aside>
        <h1>PDF Extractor</h1>
        <p className="sub">Deterministic PDF extraction · answers by Claude</p>

        <div
          className="drop"
          onClick={() => fileRef.current.click()}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            onFiles(e.dataTransfer.files);
          }}
        >
          {uploading ? "Uploading…" : "Drop PDFs here or click to browse"}
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf"
            multiple
            hidden
            onChange={(e) => {
              onFiles(e.target.files);
              e.target.value = "";
            }}
          />
        </div>

        <ul className="files">
          {docs.map((d) => (
            <li key={d.id}>
              <div>
                <strong>{d.name}</strong>
                <small>
                  {statusText(d)}
                </small>
              </div>
              <div className="filebtns">
                {isUsable(d) && (
                  <>
                    <a href={exportUrl(d.id, "markdown")} target="_blank" rel="noreferrer">md</a>
                    <a href={exportUrl(d.id, "json")} target="_blank" rel="noreferrer">json</a>
                    <a href={exportUrl(d.id, "tables")} target="_blank" rel="noreferrer">tables</a>
                  </>
                )}
                {d.error && <button onClick={() => retry(d.id)}>Retry</button>}
                <button className="x" onClick={() => remove(d.id)} title="Remove">×</button>
              </div>
            </li>
          ))}
          {!docs.length && <li className="empty">No PDFs yet.</li>}
        </ul>
        {error && <div className="warn">{error}</div>}
      </aside>

      <main>
        {cards.map((c, idx) => (
          <section className="card" key={c.id}>
            <div className="cardhead">
              <h2>Extraction {idx + 1}</h2>
              {cards.length > 1 && (
                <button className="x" onClick={() => setCards((cs) => cs.filter((x) => x.id !== c.id))}>
                  ×
                </button>
              )}
            </div>

            <label>What information do you want?</label>
            <textarea
              rows={3}
              placeholder="e.g. Invoice number, total amount, due date, vendor name"
              value={c.instruction}
              onChange={(e) => patch(c.id, { instruction: e.target.value })}
            />

            <label>From which PDFs?</label>
            <div className="checks">
              {choosable.length === 0 && <span className="muted">Upload a PDF to get started.</span>}
              {choosable.map((d) => (
                <label key={d.id} className="check">
                  <input
                    type="checkbox"
                    checked={c.docIds.includes(d.id)}
                    onChange={() => toggleDoc(c, d.id)}
                  />
                  {d.name}
                  {d.status === "processing" && <span className="muted">(reading…)</span>}
                  {d.status === "ready_text" && <span className="muted">(second check running)</span>}
                </label>
              ))}
            </div>

            <label>Output format</label>
            <div className="row">
              <select value={c.format} onChange={(e) => patch(c.id, { format: e.target.value })}>
                {FORMATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              {c.format === "custom" && (
                <input
                  placeholder="Describe the format"
                  value={c.custom}
                  onChange={(e) => patch(c.id, { custom: e.target.value })}
                />
              )}
              <button className="primary" disabled={c.loading} onClick={() => run(c)}>
                {c.loading ? "Extracting…" : "Extract"}
              </button>
            </div>

            {c.error && <div className="warn">{c.error}</div>}
            {c.result && (
              <ResultBoundary result={c.result}>
                <Result result={c.result} />
              </ResultBoundary>
            )}
          </section>
        ))}

        <div className="footer">
          <button onClick={() => setCards((cs) => [...cs, newCard()])}>+ Add more information</button>
          {cards.length > 1 && <button className="primary" onClick={runAll}>Extract all</button>}
        </div>
      </main>
    </div>
  );
}
