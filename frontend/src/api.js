// 后端 API 客户端。基址可由 VITE_API_BASE_URL 覆盖；
// 默认同源（生产由 nginx 反代 /api 到 API 容器）。
const BASE = import.meta.env.VITE_API_BASE_URL ?? "";

async function parseJson(res) {
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* 忽略非 JSON 响应体 */
  }
  return data;
}

export async function fetchLayout() {
  const res = await fetch(`${BASE}/api/layout`);
  if (!res.ok) throw new Error(`加载方案失败：${res.status}`);
  return res.json();
}

export async function fetchDefects() {
  const res = await fetch(`${BASE}/api/defects`);
  if (!res.ok) throw new Error(`加载瑕疵区失败：${res.status}`);
  return res.json();
}

// 返回 {ok: true, data} 或 {ok: false, status, detail, fieldErrors}
// gridStep 随布局一起提交保存；旧后端/旧记录缺省按 1 毫米处理
export async function submitLayout(windows, gridStep = 1) {
  const payload = {
    grid_step: gridStep,
    windows: windows.map((w) => {
      const label = (w.label ?? "").trim();
      return {
        x: w.x,
        y: w.y,
        w: w.w,
        h: w.h,
        // 空编号不发送（后端按空值处理），与旧客户端请求形状保持一致
        ...(label === "" ? {} : { label }),
      };
    }),
  };
  const res = await fetch(`${BASE}/api/layout`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await parseJson(res);
  if (res.ok) return { ok: true, data };
  return {
    ok: false,
    status: res.status,
    detail: data?.detail ?? `提交失败：${res.status}`,
    fieldErrors: data?.field_errors ?? [],
  };
}
