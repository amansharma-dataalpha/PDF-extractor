const BASE = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function check(res) {
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const detail = (await res.json()).detail;
      // FastAPI validation errors (422) send detail as a list of objects.
      if (detail) msg = typeof detail === "string" ? detail : JSON.stringify(detail);
    } catch {}
    throw new Error(msg);
  }
  return res;
}

export async function listDocs() {
  return (await check(await fetch(`${BASE}/documents`))).json();
}

export async function uploadDocs(files) {
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  return (await check(await fetch(`${BASE}/documents`, { method: "POST", body: fd }))).json();
}

export async function deleteDoc(id) {
  await check(await fetch(`${BASE}/documents/${id}`, { method: "DELETE" }));
}

export async function retryDoc(id) {
  return (await check(await fetch(`${BASE}/documents/${id}/retry`, { method: "POST" }))).json();
}

export async function extract(body) {
  return (
    await check(
      await fetch(`${BASE}/extract`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
    )
  ).json();
}

export const exportUrl = (id, format) => `${BASE}/documents/${id}/export?format=${format}`;
