import { useEffect, useMemo, useState } from "react";
import SheetCanvas from "./components/SheetCanvas.jsx";
import WindowForm from "./components/WindowForm.jsx";
import { fetchDefects, fetchLayout, submitLayout } from "./api.js";
import {
  DEFECTS,
  DEFAULT_GRID_STEP,
  GRID_STEPS,
  INNER_BOTTOM,
  INNER_RIGHT,
  MARGIN,
  SHEET_HEIGHT,
  SHEET_WIDTH,
  VERDICT_CUTTABLE,
  VERDICT_REJECTED,
  adjudicate,
  fieldErrors,
  gridFieldErrors,
  lastPositionTick,
  normalizeGridStep,
  snapEdge,
  snapPosition,
  snapSize,
  windowName,
} from "./geometry.js";

let tmpCounter = 0;
const nextTmpId = () => `tmp-${++tmpCounter}`;

function clientToMm(svg, event) {
  const ctm = svg.getScreenCTM();
  const pt = new DOMPoint(event.clientX, event.clientY).matrixTransform(ctm.inverse());
  return { x: pt.x, y: pt.y };
}

// 拖拽起点/终点吸附到安全内区刻度（以左上角为基准的自由边，可到内区右/下边界）
function snapCanvasPoint(x, y, step) {
  return [snapEdge(x, step, INNER_RIGHT), snapEdge(y, step, INNER_BOTTOM)];
}

export default function App() {
  const [defects, setDefects] = useState(
    DEFECTS.map((d, index) => ({ index, ...d }))
  );
  const [windows, setWindows] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [draftRect, setDraftRect] = useState(null); // 正在拖放的新开窗
  const [saved, setSaved] = useState(null); // 最近一次服务器裁决
  const [dirty, setDirty] = useState(false);
  const [submitRows, setSubmitRows] = useState([]); // 422 逐字段错误
  const [banner, setBanner] = useState(null); // {type,text}
  const [loading, setLoading] = useState(true);
  // 当前定位步长（1/5/10 毫米）；随布局提交保存、读取后恢复
  const [gridStep, setGridStep] = useState(DEFAULT_GRID_STEP);

  useEffect(() => {
    let alive = true;
    Promise.all([fetchDefects(), fetchLayout()])
      .then(([d, layout]) => {
        if (!alive) return;
        if (d.defects?.length) setDefects(d.defects.map((z) => ({ ...z })));
        const restoredStep = normalizeGridStep(layout.grid_step);
        if (restoredStep !== null) setGridStep(restoredStep);
        if (layout.windows.length > 0) {
          setWindows(
            [...layout.windows]
              .sort((a, b) => a.position - b.position)
              .map(({ id, x, y, w, h, label }) => ({ id, x, y, w, h, label: label ?? "" }))
          );
          setSaved({ verdict: layout.verdict, result: layout.result });
        }
      })
      .catch(() => setBanner({ type: "error", text: "无法连接后端 API" }))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  // 本地即时预览裁决（提交以后端结果为准）
  const verdictData = useMemo(() => adjudicate(windows), [windows]);

  // 本地字段非法（越过安全区等）时也不能给出“可裁切”。
  // 已保存视图信任服务器裁决；只有草稿（dirty）时才把当前步长下
  // 偏离刻度的开窗也标记为非法——切换步长本身不改坐标、不改原裁决。
  const localInvalidIds = useMemo(
    () =>
      new Set(
        windows
          .filter((win) => {
            if (Object.keys(fieldErrors(win.x, win.y, win.w, win.h)).length > 0) return true;
            if (dirty && Object.keys(gridFieldErrors(win.x, win.y, win.w, win.h, gridStep)).length > 0) {
              return true;
            }
            return false;
          })
          .map((w) => w.id)
      ),
    [windows, dirty, gridStep]
  );

  // 页面上唯一的结论：未提交且有改动时显示“未保存预览”
  // 两种视图统一成 {verdict, result} 结构，result 内含完整冲突明细
  const isPreview = dirty || !saved;
  const previewVerdict =
    localInvalidIds.size > 0 ? VERDICT_REJECTED : verdictData.verdict;
  const verdictView = isPreview
    ? { verdict: previewVerdict, result: verdictData }
    : { verdict: saved.verdict, result: saved.result };
  const conflictingIds = useMemo(
    () => new Set(verdictView.result?.conflicting_window_ids ?? []),
    [verdictView]
  );
  const hitDefects = useMemo(
    () =>
      new Set(
        (verdictView.result?.defect_conflicts ?? []).map((c) => c.defect_index)
      ),
    [verdictView]
  );

  // 开窗 id → 展示名（编号或顺序号），冲突说明按编号指认每一扇窗
  const nameById = useMemo(() => {
    const m = new Map();
    windows.forEach((w, i) => m.set(w.id, windowName(w, i)));
    return m;
  }, [windows]);

  // 冲突说明：逐条列出哪扇窗侵入瑕疵区、哪两扇窗相互重叠
  const conflictLines = useMemo(() => {
    const result = verdictView.result;
    if (!result) return [];
    const nameOf = (id) => nameById.get(id) ?? `#${id}`;
    const lines = (result.defect_conflicts ?? []).map(
      (c) => `开窗 ${nameOf(c.window_id)} 侵入瑕疵区 ${c.defect_index + 1}`
    );
    (result.window_conflicts ?? []).forEach((c) => {
      lines.push(`开窗 ${nameOf(c.window_a)} 与 开窗 ${nameOf(c.window_b)} 相互重叠`);
    });
    return lines;
  }, [verdictView, nameById]);

  // 每个开窗的服务器字段错误（按提交次序对应行号）
  const rowErrorMap = useMemo(() => {
    const m = new Map();
    submitRows.forEach((row) => m.set(windows[row.index]?.id, row.fields));
    return m;
  }, [submitRows, windows]);

  function markDirty() {
    setDirty(true);
    setSubmitRows([]);
  }

  function updateWindow(id, patch) {
    setWindows((list) =>
      list.map((w) => {
        if (w.id !== id) return w;
        const next = { ...w, ...patch };
        // 表单编辑按当前步长即时吸附：位置在“容得下整窗的行程”内取最近
        // 刻度（靠近右/下侧时回退），尺寸取最近合法倍数且不越过安全区。
        if (patch.x !== undefined) {
          next.x = snapPosition(patch.x, gridStep, INNER_RIGHT - MARGIN - w.w);
        }
        if (patch.y !== undefined) {
          next.y = snapPosition(patch.y, gridStep, INNER_BOTTOM - MARGIN - w.h);
        }
        if (patch.w !== undefined) {
          next.w = snapSize(patch.w, gridStep, INNER_RIGHT - next.x);
          // 尺寸变大后原位置若容不下整窗（如原位置本就是最末刻度），
          // 位置回退到最后一个容得下整窗的刻度，仍是对齐刻度。
          if (next.x + next.w > INNER_RIGHT) {
            next.x = lastPositionTick(next.w, gridStep, INNER_RIGHT);
          }
        }
        if (patch.h !== undefined) {
          next.h = snapSize(patch.h, gridStep, INNER_BOTTOM - next.y);
          if (next.y + next.h > INNER_BOTTOM) {
            next.y = lastPositionTick(next.h, gridStep, INNER_BOTTOM);
          }
        }
        return next;
      })
    );
    markDirty();
  }

  // 拖动整窗：左上角在容得下整窗的行程内吸附到最近刻度
  function moveWindow(id, x, y) {
    setWindows((list) =>
      list.map((w) =>
        w.id === id
          ? {
              ...w,
              x: snapPosition(x, gridStep, INNER_RIGHT - MARGIN - w.w),
              y: snapPosition(y, gridStep, INNER_BOTTOM - MARGIN - w.h),
            }
          : w
      )
    );
    markDirty();
  }

  function addWindow(rect) {
    setWindows((list) => [...list, { id: nextTmpId(), label: "", ...rect }]);
    setSelectedId(null);
    markDirty();
  }

  function deleteWindow(id) {
    setWindows((list) => list.filter((w) => w.id !== id));
    setSelectedId((cur) => (cur === id ? null : cur));
    markDirty();
  }

  // ---- 在纸面空白处（含瑕疵区上方）拖放新开窗 ----
  function handleDrawStart(event) {
    // 只响应鼠标主键（button 0）；右键、中键不发起开窗
    if (event.button !== 0) return;
    const svg = event.currentTarget;
    // 落在已有开窗上的指针事件已被开窗自身 stopPropagation，
    // 因此能冒泡到画布的目标（纸面、瑕疵区矩形及其文字）一律视为空白处，
    // 从瑕疵区内起拖同样可以新建开窗并在松手后即时显示冲突。
    setSelectedId(null);
    const start = clientToMm(svg, event);
    // 起终点保留原始毫米（小数），由吸附函数按当前步长取最近刻度：
    // 这样“恰好居中取较小值”对半刻度位置也成立。
    const [sx, sy] = snapCanvasPoint(start.x, start.y, gridStep);
    let current = null;

    function onMove(ev) {
      const p = clientToMm(svg, ev);
      const [ex, ey] = snapCanvasPoint(p.x, p.y, gridStep);
      // 两端都是合法刻度，得到的左上角与宽高自然对齐同一刻度
      current = {
        x: Math.min(sx, ex),
        y: Math.min(sy, ey),
        w: Math.abs(ex - sx),
        h: Math.abs(ey - sy),
      };
      setDraftRect(current);
    }
    function stop() {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
    }
    function onUp() {
      stop();
      setDraftRect(null);
      if (current && current.w >= 1 && current.h >= 1) {
        addWindow(current);
      }
    }
    // 设备（触屏/手写笔/系统手势）触发指针取消：立即终止拖放并清除草稿，不新增开窗
    function onCancel() {
      stop();
      setDraftRect(null);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
  }

  async function handleSubmit() {
    setBanner(null);
    const result = await submitLayout(windows, gridStep);
    if (result.ok) {
      const { data } = result;
      setWindows(
        [...data.windows]
          .sort((a, b) => a.position - b.position)
          .map(({ id, x, y, w, h, label }) => ({ id, x, y, w, h, label: label ?? "" }))
      );
      // 保存返回的步长（旧记录/旧后端缺省为 1）恢复选择项与画布
      const savedStep = normalizeGridStep(data.grid_step);
      if (savedStep !== null) setGridStep(savedStep);
      setSaved({ verdict: data.verdict, result: data.result });
      setDirty(false);
      setSubmitRows([]);
      setSelectedId(null); // 临时 id 已被数据库 id 取代，清掉失效选中
      setBanner({ type: "ok", text: "方案已保存" });
    } else {
      // 后端逐字段打回：草稿、步长选择项与即时裁决全部保留，修正后可直接重试
      setSubmitRows(result.fieldErrors);
      setBanner({ type: "error", text: result.detail });
    }
  }

  const selected = windows.find((w) => w.id === selectedId) ?? null;
  const cuttable = verdictView.verdict === VERDICT_CUTTABLE;

  return (
    <div className="app">
      <header className="app-header">
        <h1>档案装裱排版校验台</h1>
        <p className="subtitle">
          纸张 {SHEET_WIDTH}×{SHEET_HEIGHT} 毫米 · 左上原点 · 半开矩形 ·
          压边安全区 {MARGIN} 毫米
        </p>
      </header>

      <div
        className={`verdict ${cuttable ? "verdict-ok" : "verdict-bad"}`}
        role="status"
        data-testid="verdict-banner"
        data-verdict={verdictView.verdict}
      >
        <span className="verdict-word">{verdictView.verdict}</span>
        <span className="verdict-note">
          {isPreview
            ? "（未保存预览，提交后生效）"
            : windows.length === 0
              ? "（空方案）"
              : "（已保存的裁决结果）"}
        </span>
      </div>

      {conflictLines.length > 0 ? (
        <ul className="conflict-list" data-testid="conflict-list">
          {conflictLines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : null}

      {banner ? (
        <p className={banner.type === "ok" ? "banner-ok" : "banner-error"}>{banner.text}</p>
      ) : null}

      <main className="layout">
        <section className="canvas-wrap">
          <div className="step-bar" data-testid="step-selector" role="group" aria-label="定位步长">
            <span>定位步长</span>
            {GRID_STEPS.map((step) => (
              <button
                key={step}
                type="button"
                className={gridStep === step ? "step-btn step-btn-on" : "step-btn"}
                aria-pressed={gridStep === step}
                data-testid={`step-btn-${step}`}
                onClick={() => {
                  // 仅更换量尺精度：不改坐标、不打断原裁决展示；
                  // 之后任意拖放/移动/表单编辑都会按新步长吸附并进入预览。
                  setGridStep(step);
                }}
              >
                {step} 毫米
              </button>
            ))}
          </div>
          <SheetCanvas
            defects={defects}
            windows={draftRect ? [...windows, { id: "__draft__", ...draftRect }] : windows}
            selectedId={selectedId}
            conflictingIds={[...conflictingIds, ...localInvalidIds]}
            hitDefects={[...hitDefects]}
            onSelect={setSelectedId}
            onMove={moveWindow}
            onDrawStart={handleDrawStart}
          />
          <p className="hint">在纸面空白处按住拖放可新开窗；拖动开窗可移动。坐标与尺寸按所选步长即时吸附到安全内区刻度（以左上角为基准）。</p>
        </section>

        <aside className="panel">
          <WindowForm
            win={selected}
            serverErrors={selected ? rowErrorMap.get(selected.id) : undefined}
            onChange={updateWindow}
            onDelete={deleteWindow}
          />

          <h2>开窗列表（{windows.length}）</h2>
          <ul className="window-list">
            {windows.map((w, i) => {
              const errs = fieldErrors(w.x, w.y, w.w, w.h);
              // 有待提交改动时，当前步长下偏离刻度的字段同样即时标出
              const gridErrs = dirty
                ? gridFieldErrors(w.x, w.y, w.w, w.h, gridStep)
                : {};
              const serverErrs = rowErrorMap.get(w.id);
              const bad = conflictingIds.has(w.id) || localInvalidIds.has(w.id);
              return (
                <li
                  key={w.id}
                  className={bad ? "row row-conflict" : "row"}
                  data-testid={`list-row-${i}`}
                >
                  <button
                    type="button"
                    className="row-select"
                    onClick={() => setSelectedId(w.id)}
                  >
                    {windowName(w, i)} ({w.x}, {w.y}) {w.w}×{w.h}
                  </button>
                  {Object.entries({ ...errs, ...gridErrs, ...(serverErrs ?? {}) }).map(([k, v]) => (
                    <small key={k} className="error-text">
                      {k === "label" ? "编号" : k}: {v}
                    </small>
                  ))}
                </li>
              );
            })}
          </ul>

          <button
            type="button"
            className="btn-primary"
            onClick={handleSubmit}
            disabled={loading || windows.length === 0}
            data-testid="submit-btn"
          >
            提交方案并裁决
          </button>
          {submitRows.length > 0 ? (
            <p className="banner-error" data-testid="submit-errors">
              存在 {submitRows.length} 个非法开窗，整次提交未保存。
            </p>
          ) : null}
        </aside>
      </main>

      <footer className="legend">
        <span><i className="sw sw-defect" /> 内置只读瑕疵区</span>
        <span><i className="sw sw-window" /> 开窗</span>
        <span><i className="sw sw-conflict" /> 冲突高亮</span>
      </footer>
    </div>
  );
}
