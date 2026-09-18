import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App.jsx";

vi.mock("../api.js", () => ({
  fetchDefects: vi.fn(),
  fetchLayout: vi.fn(),
  submitLayout: vi.fn(),
}));

import { fetchDefects, fetchLayout, submitLayout } from "../api.js";

const cleanResult = {
  defect_conflicts: [],
  window_conflicts: [],
  conflicting_window_ids: [],
};

// jsdom 的 PointerEvent init 不携带 clientX/clientY，
// 这里用 MouseEvent 派发 pointer* 类型事件（React 按 type 绑定监听）。
function firePointer(node, type, x, y, button = 0) {
  const ev = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: x,
    clientY: y,
    button,
  });
  node.dispatchEvent(ev);
}

function dragDraw(svg, x1, y1, x2, y2) {
  firePointer(svg, "pointerdown", x1, y1);
  firePointer(window, "pointermove", x2, y2);
  firePointer(window, "pointerup", x2, y2);
}

// 在指定画布元素（如瑕疵区矩形）上按下，其余事件仍派发到 window
function dragDrawFrom(startTarget, x1, y1, x2, y2, button = 0) {
  firePointer(startTarget, "pointerdown", x1, y1, button);
  firePointer(window, "pointermove", x2, y2);
  firePointer(window, "pointerup", x2, y2, button);
}

beforeEach(() => {
  vi.mocked(fetchDefects).mockResolvedValue({
    defects: [
      { index: 0, x: 200, y: 150, w: 80, h: 40 },
      { index: 1, x: 620, y: 420, w: 60, h: 90 },
    ],
    sheet: { width: 1000, height: 700, margin: 12 },
  });
  vi.mocked(fetchLayout).mockResolvedValue({
    verdict: null,
    windows: [],
    result: null,
    created_at: null,
  });
  vi.mocked(submitLayout).mockReset();
});

describe("加载与恢复", () => {
  it("刷新后恢复同一布局并显示已保存裁决", async () => {
    vi.mocked(fetchLayout).mockResolvedValue({
      verdict: "不可裁切",
      windows: [{ id: 11, position: 0, x: 250, y: 170, w: 60, h: 30 }],
      result: {
        verdict: "不可裁切",
        defect_conflicts: [{ window_id: 11, defect_index: 0 }],
        window_conflicts: [],
        conflicting_window_ids: [11],
      },
      created_at: "2026-09-14T00:00:00Z",
    });
    render(<App />);

    await screen.findByText(/#1 \(250, 170\) 60×30/);
    const banner = await screen.findByTestId("verdict-banner");
    expect(banner).toHaveAttribute("data-verdict", "不可裁切");
    expect(banner.textContent).toContain("已保存");
    // 冲突开窗在列表中高亮
    const row = await screen.findByTestId("list-row-0");
    expect(row.className).toContain("row-conflict");
  });
});

describe("拖放开窗与即时裁决", () => {
  it("在空白区拖放开窗，干净方案即时显示可裁切", async () => {
    render(<App />);
    await screen.findByText(/提交方案并裁决/);
    const svg = document.querySelector(".sheet");

    dragDraw(svg, 300, 300, 400, 360);

    expect(await screen.findByText(/#1 \(300, 300\) 100×60/)).toBeInTheDocument();
    const banner = screen.getByTestId("verdict-banner");
    expect(banner).toHaveAttribute("data-verdict", "可裁切");
    expect(banner.textContent).toContain("未保存预览");
    // 干净开窗不高亮
    expect(document.querySelector(".window-conflict")).toBeNull();
  });

  it("开窗压到瑕疵区即时显示不可裁切并高亮全部冲突", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    // 侵入第一个瑕疵区 [200,280)×[150,190)
    dragDraw(svg, 220, 160, 260, 185);

    const banner = await screen.findByTestId("verdict-banner");
    expect(banner).toHaveAttribute("data-verdict", "不可裁切");
    expect(document.querySelector(".window-conflict")).not.toBeNull();
    expect(document.querySelector(".defect-hit")).not.toBeNull();
  });

  it("边线相贴不算冲突", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    // 左边线贴瑕疵区右边线 x=280
    dragDraw(svg, 280, 150, 320, 180);
    await screen.findByText(/#1 \(280, 150\) 40×30/);
    expect(screen.getByTestId("verdict-banner")).toHaveAttribute("data-verdict", "可裁切");
  });

  it("从瑕疵区内起拖也能新建开窗，并即时显示冲突", async () => {
    render(<App />);
    await screen.findByText(/提交方案并裁决/);
    // 直接在瑕疵区矩形（而非纸面/画布根节点）上按下指针
    const defect = document.querySelectorAll(".defect")[0];
    // 拖放范围完全落在第一个瑕疵区 [200,280)×[150,190) 内
    dragDrawFrom(defect, 210, 160, 260, 185);

    expect(await screen.findByText(/#1 \(210, 160\) 50×25/)).toBeInTheDocument();
    const banner = screen.getByTestId("verdict-banner");
    expect(banner).toHaveAttribute("data-verdict", "不可裁切");
    expect(document.querySelector(".window-conflict")).not.toBeNull();
    expect(document.querySelector(".defect-hit")).not.toBeNull();
  });

  it("右键拖放不会新建观察窗", () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    act(() => {
      // button=2 为鼠标右键
      dragDrawFrom(svg, 300, 300, 400, 360, 2);
    });

    expect(document.querySelector(".window-draft")).toBeNull();
    expect(document.querySelector(".window")).toBeNull();
    expect(screen.queryByText(/#1 /)).not.toBeInTheDocument();
  });

  it("右键拖动已有开窗不会移动它", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 300, 300, 400, 360);
    await screen.findByText(/#1 \(300, 300\) 100×60/);

    const win = document.querySelector(".window");
    act(() => {
      firePointer(win, "pointerdown", 350, 330, 2);
      firePointer(window, "pointermove", 500, 500, 2);
      firePointer(window, "pointerup", 500, 500, 2);
    });

    expect(screen.getByText(/#1 \(300, 300\) 100×60/)).toBeInTheDocument();
  });

  it("拖放中收到指针取消：立即清除草稿，之后移动不再跟随且不新增开窗", () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    // 主键开始拖放并移动一次，草稿出现
    act(() => {
      firePointer(svg, "pointerdown", 300, 300);
      firePointer(window, "pointermove", 360, 360);
    });
    expect(document.querySelector(".window-draft")).not.toBeNull();

    // 设备触发 pointercancel：草稿立即消失
    act(() => {
      firePointer(window, "pointercancel", 360, 360);
    });
    expect(document.querySelector(".window-draft")).toBeNull();

    // 取消后指针继续移动：草稿不得重新出现或跟随
    act(() => {
      firePointer(window, "pointermove", 450, 450);
    });
    expect(document.querySelector(".window-draft")).toBeNull();

    // 取消后即使再松手也不会补建开窗
    act(() => {
      firePointer(window, "pointerup", 450, 450);
    });
    expect(document.querySelector(".window")).toBeNull();
    expect(screen.queryByText(/#1 /)).not.toBeInTheDocument();
  });
});

describe("表单编辑", () => {
  it("选中后填写 x/y/宽/高，失焦生效并重新裁决", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 300, 300, 400, 360);
    fireEvent.click(await screen.findByText(/#1 \(300, 300\) 100×60/));

    const xInput = screen.getByLabelText("x（毫米）");
    fireEvent.change(xInput, { target: { value: "500" } });
    fireEvent.blur(xInput);

    await screen.findByText(/#1 \(500, 300\) 100×60/);
  });

  it("非整数输入被忽略", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 300, 300, 400, 360);
    fireEvent.click(await screen.findByText(/#1 \(300, 300\) 100×60/));

    const wInput = screen.getByLabelText("宽（毫米）");
    fireEvent.change(wInput, { target: { value: "abc" } });
    fireEvent.blur(wInput);
    expect(await screen.findByText(/#1 \(300, 300\) 100×60/)).toBeInTheDocument();
  });

  it("表单输入越过安全区时回退吸附到容得下整窗的最后刻度（1 毫米步长）", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 300, 300, 400, 360);
    expect((await screen.findByTestId("verdict-banner")).getAttribute("data-verdict")).toBe(
      "可裁切"
    );
    fireEvent.click(screen.getByText(/#1 \(300, 300\) 100×60/));

    // 950 + 宽 100 = 1050 > 988：位置回退到最后一个容得下整窗的刻度 x=888
    const xInput = screen.getByLabelText("x（毫米）");
    fireEvent.change(xInput, { target: { value: "950" } });
    fireEvent.blur(xInput);

    await screen.findByText(/#1 \(888, 300\) 100×60/);
    expect(screen.getByTestId("verdict-banner")).toHaveAttribute("data-verdict", "可裁切");
    expect(document.querySelector(".window-conflict")).toBeNull();
  });
});

describe("提交与逐字段错误", () => {
  it("合法提交成功后展示唯一的已保存可裁切结论", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 300, 300, 400, 360);
    await screen.findByText(/#1 \(300, 300\) 100×60/);

    vi.mocked(submitLayout).mockResolvedValue({
      ok: true,
      data: {
        verdict: "可裁切",
        windows: [{ id: 1, position: 0, x: 300, y: 300, w: 100, h: 60 }],
        result: { verdict: "可裁切", ...cleanResult },
        created_at: "2026-09-14T00:00:00Z",
      },
    });

    fireEvent.click(screen.getByTestId("submit-btn"));

    await waitFor(() =>
      expect(screen.getByTestId("verdict-banner").textContent).toContain("已保存")
    );
    expect(submitLayout).toHaveBeenCalledWith(
      [{ id: expect.any(String), x: 300, y: 300, w: 100, h: 60, label: "" }],
      1
    );
  });

  it("任一字段非法时显示逐字段错误且整次不落库提示", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 300, 300, 400, 360);
    await screen.findByText(/#1 \(300, 300\) 100×60/);

    vi.mocked(submitLayout).mockResolvedValue({
      ok: false,
      status: 422,
      detail: "存在非法开窗，本次提交未保存",
      fieldErrors: [
        { index: 0, fields: { x: "x 必须 ≥ 12（压边安全区）" } },
      ],
    });

    fireEvent.click(screen.getByTestId("submit-btn"));

    const errors = await screen.findByTestId("submit-errors");
    expect(errors.textContent).toContain("整次提交未保存");
    const row = screen.getByTestId("list-row-0");
    expect(within(row).getByText(/x 必须 ≥ 12/)).toBeInTheDocument();
    // 仍然停留在未保存预览状态
    expect(screen.getByTestId("verdict-banner").textContent).toContain("未保存预览");
  });
});

describe("工件编号", () => {
  it("恢复带编号的布局：画布与列表用编号，空编号用顺序号", async () => {
    vi.mocked(fetchLayout).mockResolvedValue({
      verdict: "可裁切",
      windows: [
        { id: 11, position: 0, x: 300, y: 300, w: 100, h: 80, label: "ZW-001" },
        { id: 12, position: 1, x: 500, y: 100, w: 40, h: 40, label: null },
      ],
      result: { verdict: "可裁切", ...cleanResult },
      created_at: "2026-09-14T00:00:00Z",
    });
    render(<App />);

    // 列表：有编号用编号，空编号退回顺序号
    await screen.findByText(/ZW-001 \(300, 300\) 100×80/);
    await screen.findByText(/#2 \(500, 100\) 40×40/);
    // 画布上的开窗文字同样使用编号/顺序号
    const svg = document.querySelector(".sheet");
    expect(within(svg).getByText("ZW-001")).toBeInTheDocument();
    expect(within(svg).getByText("#2")).toBeInTheDocument();
  });

  it("在表单录入编号后随开窗一起提交，保存后仍显示编号", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 300, 300, 400, 360);
    fireEvent.click(await screen.findByText(/#1 \(300, 300\) 100×60/));

    const labelInput = screen.getByLabelText("工件编号");
    fireEvent.change(labelInput, { target: { value: "  ZW-001  " } });
    fireEvent.blur(labelInput);

    // 列表与画布即时改用编号（去首尾空格）
    await screen.findByText(/ZW-001 \(300, 300\) 100×60/);
    expect(within(document.querySelector(".sheet")).getByText("ZW-001")).toBeInTheDocument();

    vi.mocked(submitLayout).mockResolvedValue({
      ok: true,
      data: {
        verdict: "可裁切",
        windows: [{ id: 1, position: 0, x: 300, y: 300, w: 100, h: 60, label: "ZW-001" }],
        result: { verdict: "可裁切", ...cleanResult },
        created_at: "2026-09-14T00:00:00Z",
      },
    });
    fireEvent.click(screen.getByTestId("submit-btn"));

    await waitFor(() =>
      expect(screen.getByTestId("verdict-banner").textContent).toContain("已保存")
    );
    expect(submitLayout).toHaveBeenCalledWith(
      [{ id: expect.any(String), x: 300, y: 300, w: 100, h: 60, label: "ZW-001" }],
      1
    );
    // 保存后编号仍在列表中
    expect(screen.getByText(/ZW-001 \(300, 300\) 100×60/)).toBeInTheDocument();
  });

  it("编号只填空格视为未填写，仍显示顺序号", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 300, 300, 400, 360);
    fireEvent.click(await screen.findByText(/#1 \(300, 300\) 100×60/));

    const labelInput = screen.getByLabelText("工件编号");
    fireEvent.change(labelInput, { target: { value: "   " } });
    fireEvent.blur(labelInput);

    expect(await screen.findByText(/#1 \(300, 300\) 100×60/)).toBeInTheDocument();
    expect(within(document.querySelector(".sheet")).getByText("#1")).toBeInTheDocument();
  });

  it("重复编号被 422 逐行打回：草稿、选中项与冲突高亮保留，修正后可再次提交", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    // 两个相互重叠的开窗 → 即时冲突高亮
    dragDraw(svg, 300, 300, 360, 360);
    dragDraw(svg, 330, 330, 390, 390);

    // 两个开窗填上相同编号
    fireEvent.click(await screen.findByText(/#1 \(300, 300\) 60×60/));
    fireEvent.change(screen.getByLabelText("工件编号"), { target: { value: "DUP-1" } });
    fireEvent.blur(screen.getByLabelText("工件编号"));
    fireEvent.click(await screen.findByText(/#2 \(330, 330\) 60×60/));
    fireEvent.change(screen.getByLabelText("工件编号"), { target: { value: "DUP-1" } });
    fireEvent.blur(screen.getByLabelText("工件编号"));
    await screen.findByText(/DUP-1 \(330, 330\) 60×60/);

    // 提交前已有即时冲突高亮
    expect(document.querySelectorAll(".window-conflict").length).toBe(2);

    vi.mocked(submitLayout).mockResolvedValue({
      ok: false,
      status: 422,
      detail: "存在非法开窗，本次提交未保存",
      fieldErrors: [
        { index: 0, fields: { label: "编号与其他开窗重复" } },
        { index: 1, fields: { label: "编号与其他开窗重复" } },
      ],
    });
    fireEvent.click(screen.getByTestId("submit-btn"));

    // 两行都显示编号字段错误
    const row0 = await screen.findByTestId("list-row-0");
    const row1 = screen.getByTestId("list-row-1");
    await within(row0).findByText(/编号与其他开窗重复/);
    await within(row1).findByText(/编号与其他开窗重复/);

    // 草稿、选中项与即时冲突高亮全部保留
    expect(screen.getByText(/DUP-1 \(300, 300\) 60×60/)).toBeInTheDocument();
    expect(screen.getByText(/DUP-1 \(330, 330\) 60×60/)).toBeInTheDocument();
    expect(screen.getByTestId("window-form")).toBeInTheDocument();
    expect(document.querySelectorAll(".window-conflict").length).toBe(2);
    expect(screen.getByTestId("verdict-banner").textContent).toContain("未保存预览");

    // 修正第二个开窗的编号后直接再次提交
    fireEvent.change(screen.getByLabelText("工件编号"), { target: { value: "ZW-002" } });
    fireEvent.blur(screen.getByLabelText("工件编号"));
    await screen.findByText(/ZW-002 \(330, 330\) 60×60/);

    vi.mocked(submitLayout).mockResolvedValue({
      ok: true,
      data: {
        verdict: "不可裁切",
        windows: [
          { id: 21, position: 0, x: 300, y: 300, w: 60, h: 60, label: "DUP-1" },
          { id: 22, position: 1, x: 330, y: 330, w: 60, h: 60, label: "ZW-002" },
        ],
        result: {
          verdict: "不可裁切",
          defect_conflicts: [],
          window_conflicts: [{ window_a: 21, window_b: 22 }],
          conflicting_window_ids: [21, 22],
        },
        created_at: "2026-09-14T00:00:00Z",
      },
    });
    fireEvent.click(screen.getByTestId("submit-btn"));

    await waitFor(() =>
      expect(screen.getByTestId("verdict-banner").textContent).toContain("已保存")
    );
    expect(submitLayout).toHaveBeenLastCalledWith(
      [
        { id: expect.any(String), x: 300, y: 300, w: 60, h: 60, label: "DUP-1" },
        { id: expect.any(String), x: 330, y: 330, w: 60, h: 60, label: "ZW-002" },
      ],
      1
    );
  });

  it("带编号的重叠开窗：画布与冲突说明都用编号指认，保存后仍对应", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 300, 300, 360, 360);
    dragDraw(svg, 330, 330, 390, 390);

    fireEvent.click(await screen.findByText(/#1 \(300, 300\) 60×60/));
    fireEvent.change(screen.getByLabelText("工件编号"), { target: { value: "WIN-A" } });
    fireEvent.blur(screen.getByLabelText("工件编号"));
    fireEvent.click(await screen.findByText(/#2 \(330, 330\) 60×60/));
    fireEvent.change(screen.getByLabelText("工件编号"), { target: { value: "WIN-B" } });
    fireEvent.blur(screen.getByLabelText("工件编号"));
    await screen.findByText(/WIN-B \(330, 330\) 60×60/);

    // 画布上的开窗文字使用编号
    const canvas = document.querySelector(".sheet");
    expect(within(canvas).getByText("WIN-A")).toBeInTheDocument();
    expect(within(canvas).getByText("WIN-B")).toBeInTheDocument();

    // 冲突说明用编号指认双方
    const list = await screen.findByTestId("conflict-list");
    expect(list.textContent).toContain("开窗 WIN-A 与 开窗 WIN-B 相互重叠");

    // 保存（不可裁切）后冲突说明仍与编号对应
    vi.mocked(submitLayout).mockResolvedValue({
      ok: true,
      data: {
        verdict: "不可裁切",
        windows: [
          { id: 31, position: 0, x: 300, y: 300, w: 60, h: 60, label: "WIN-A" },
          { id: 32, position: 1, x: 330, y: 330, w: 60, h: 60, label: "WIN-B" },
        ],
        result: {
          verdict: "不可裁切",
          defect_conflicts: [],
          window_conflicts: [{ window_a: 31, window_b: 32 }],
          conflicting_window_ids: [31, 32],
        },
        created_at: "2026-09-14T00:00:00Z",
      },
    });
    fireEvent.click(screen.getByTestId("submit-btn"));
    await waitFor(() =>
      expect(screen.getByTestId("verdict-banner").textContent).toContain("已保存")
    );
    expect(screen.getByTestId("conflict-list").textContent).toContain(
      "开窗 WIN-A 与 开窗 WIN-B 相互重叠"
    );
  });

  it("侵入瑕疵区的冲突说明同样使用编号", async () => {
    render(<App />);
    const svg = document.querySelector(".sheet");
    // 侵入第一个瑕疵区 [200,280)×[150,190)
    dragDraw(svg, 220, 160, 260, 185);
    fireEvent.click(await screen.findByText(/#1 \(220, 160\) 40×25/));
    fireEvent.change(screen.getByLabelText("工件编号"), { target: { value: "WIN-A" } });
    fireEvent.blur(screen.getByLabelText("工件编号"));

    const list = await screen.findByTestId("conflict-list");
    expect(list.textContent).toContain("开窗 WIN-A 侵入瑕疵区 1");
  });
});

describe("定位步长（1/5/10 毫米）", () => {
  it("默认选中 1 毫米，可切换到 5/10", async () => {
    render(<App />);
    await screen.findByText(/提交方案并裁决/);
    await act(async () => {}); // 等初始 GET 的 Promise 链与 state 更新全部落定
    expect(screen.getByTestId("step-btn-1")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("step-btn-5"));
    expect(screen.getByTestId("step-btn-5")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("step-btn-1")).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(screen.getByTestId("step-btn-10"));
    expect(screen.getByTestId("step-btn-10")).toHaveAttribute("aria-pressed", "true");
  });

  it("5 毫米步长下拖放开窗：非刻度起终点吸附，居中取较小值", async () => {
    render(<App />);
    await screen.findByText(/提交方案并裁决/);
    await act(async () => {}); // 等初始 GET 的 Promise 链与 state 更新全部落定
    fireEvent.click(screen.getByTestId("step-btn-5"));
    const svg = document.querySelector(".sheet");
    // 起点 (219,301)：x 219→217，y 301→302；
    // 终点 (261,336)：x 261→262，y 336→337。
    dragDraw(svg, 219, 301, 261, 336);
    expect(await screen.findByText(/#1 \(217, 302\) 45×35/)).toBeInTheDocument();
  });

  it("5 毫米步长下三种编辑方式得到完全一致的坐标", async () => {
    render(<App />);
    await screen.findByText(/提交方案并裁决/);
    await act(async () => {}); // 等初始 GET 的 Promise 链与 state 更新全部落定
    fireEvent.click(screen.getByTestId("step-btn-5"));
    const svg = document.querySelector(".sheet");

    // 方式一：拖放开窗 → (217,302) 45×35
    dragDraw(svg, 219, 301, 261, 336);
    expect(await screen.findByText(/#1 \(217, 302\) 45×35/)).toBeInTheDocument();

    // 方式二：另开一扇窗后拖动移位到同一位置
    dragDraw(svg, 12, 12, 57, 47); // (12,12) 45×35
    expect(await screen.findByText(/#2 \(12, 12\) 45×35/)).toBeInTheDocument();
    const win2 = document.querySelectorAll(".window")[1];
    // 在 #2 内部 (22,22) 处抓取（grab 偏移 10,10），拖到 (228,313)：
    // 左上角 raw = 218,303 → 吸附 217,302
    act(() => {
      firePointer(win2, "pointerdown", 22, 22);
      firePointer(window, "pointermove", 228, 313);
      firePointer(window, "pointerup", 228, 313);
    });
    expect(await screen.findByText(/#2 \(217, 302\) 45×35/)).toBeInTheDocument();

    // 方式三：第三扇窗用表单输入非刻度原始值，失焦吸附到同一坐标
    dragDraw(svg, 12, 60, 17, 65); // 12/60→12/62，17/65→17/67：得 (12,62) 5×5
    fireEvent.click(await screen.findByText(/#3 \(12, 62\) 5×5/));
    const xInput = screen.getByLabelText("x（毫米）");
    const yInput = screen.getByLabelText("y（毫米）");
    const wInput = screen.getByLabelText("宽（毫米）");
    const hInput = screen.getByLabelText("高（毫米）");
    fireEvent.change(xInput, { target: { value: "218" } }); // 218→217
    fireEvent.blur(xInput);
    fireEvent.change(yInput, { target: { value: "303" } }); // 303→302
    fireEvent.blur(yInput);
    fireEvent.change(wInput, { target: { value: "46" } }); // 46→45
    fireEvent.blur(wInput);
    fireEvent.change(hInput, { target: { value: "33" } }); // 33→35
    fireEvent.blur(hInput);
    expect(await screen.findByText(/#3 \(217, 302\) 45×35/)).toBeInTheDocument();
  });

  it("拖动时恰好处于两刻度正中间取较小值，靠近右侧回退到最后容得下整窗的刻度", async () => {
    render(<App />);
    await screen.findByText(/提交方案并裁决/);
    await act(async () => {}); // 等初始 GET 的 Promise 链与 state 更新全部落定
    fireEvent.click(screen.getByTestId("step-btn-5"));
    const svg = document.querySelector(".sheet");
    // (12,12) 45×35：行程上限 x = 988-45 = 943，最后刻度 942
    dragDraw(svg, 12, 12, 57, 47);
    await screen.findByText(/#1 \(12, 12\) 45×35/);
    const win = document.querySelector(".window");

    act(() => {
      // 抓取点即左上角（grab=0）；raw=219.5 正好在 217 与 222 中间 → 取 217
      firePointer(win, "pointerdown", 12, 12);
      firePointer(window, "pointermove", 219.5, 302);
      firePointer(window, "pointerup", 219.5, 302);
    });
    expect(await screen.findByText(/#1 \(217, 302\) 45×35/)).toBeInTheDocument();

    // 继续往右拖过行程：回退到 942（942+45=987≤988，947 则越界）
    act(() => {
      firePointer(document.querySelector(".window"), "pointerdown", 217, 302);
      firePointer(window, "pointermove", 960, 302);
      firePointer(window, "pointerup", 960, 302);
    });
    expect(await screen.findByText(/#1 \(942, 302\) 45×35/)).toBeInTheDocument();
  });

  it("5 毫米步长提交时携带 grid_step=5，保存后恢复选择项与画布", async () => {
    render(<App />);
    await screen.findByText(/提交方案并裁决/);
    await act(async () => {}); // 等初始 GET 的 Promise 链与 state 更新全部落定
    fireEvent.click(screen.getByTestId("step-btn-5"));
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 217, 302, 262, 337); // (217,302) 45×35
    await screen.findByText(/#1 \(217, 302\) 45×35/);

    vi.mocked(submitLayout).mockResolvedValue({
      ok: true,
      data: {
        verdict: "可裁切",
        grid_step: 5,
        windows: [{ id: 41, position: 0, x: 217, y: 302, w: 45, h: 35 }],
        result: { verdict: "可裁切", ...cleanResult },
        created_at: "2026-09-14T00:00:00Z",
      },
    });
    fireEvent.click(screen.getByTestId("submit-btn"));
    await waitFor(() =>
      expect(screen.getByTestId("verdict-banner").textContent).toContain("已保存")
    );
    expect(submitLayout).toHaveBeenCalledWith(
      [{ id: expect.any(String), x: 217, y: 302, w: 45, h: 35, label: "" }],
      5
    );
    // 保存后坐标相同、5 毫米选择项保持
    expect(screen.getByText(/#1 \(217, 302\) 45×35/)).toBeInTheDocument();
    expect(screen.getByTestId("step-btn-5")).toHaveAttribute("aria-pressed", "true");
  });

  it("读取旧记录（无 grid_step）按 1 毫米恢复；带 5 毫米的记录恢复 5 毫米选择与冲突高亮", async () => {
    vi.mocked(fetchLayout).mockResolvedValue({
      verdict: "不可裁切",
      grid_step: 5,
      windows: [{ id: 51, position: 0, x: 217, y: 302, w: 45, h: 35 }],
      result: {
        verdict: "不可裁切",
        defect_conflicts: [{ window_id: 51, defect_index: 0 }],
        window_conflicts: [],
        conflicting_window_ids: [51],
      },
      created_at: "2026-09-14T00:00:00Z",
    });
    render(<App />);
    await screen.findByText(/#1 \(217, 302\) 45×35/);
    expect(screen.getByTestId("step-btn-5")).toHaveAttribute("aria-pressed", "true");
    const banner = await screen.findByTestId("verdict-banner");
    expect(banner).toHaveAttribute("data-verdict", "不可裁切");
    expect(banner.textContent).toContain("已保存");
    expect(document.querySelector(".window-conflict")).not.toBeNull();
    expect(document.querySelector(".defect-hit")).not.toBeNull();
  });

  it("仅切换步长不改坐标、继续显示原裁决；随后编辑才进入预览", async () => {
    vi.mocked(fetchLayout).mockResolvedValue({
      verdict: "可裁切",
      grid_step: 1,
      windows: [{ id: 61, position: 0, x: 300, y: 300, w: 100, h: 60 }],
      result: { verdict: "可裁切", ...cleanResult },
      created_at: "2026-09-14T00:00:00Z",
    });
    render(<App />);
    await screen.findByText(/#1 \(300, 300\) 100×60/);
    expect(screen.getByTestId("verdict-banner").textContent).toContain("已保存");

    // 只切换量尺：坐标不动，裁决仍是已保存的可裁切
    fireEvent.click(screen.getByTestId("step-btn-5"));
    expect(screen.getByText(/#1 \(300, 300\) 100×60/)).toBeInTheDocument();
    const banner = screen.getByTestId("verdict-banner");
    expect(banner).toHaveAttribute("data-verdict", "可裁切");
    expect(banner.textContent).toContain("已保存");

    // 之后拖放开窗才进入未保存预览
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 217, 102, 262, 147);
    await screen.findByText(/#2 \(217, 102\) 45×45/);
    expect(screen.getByTestId("verdict-banner").textContent).toContain("未保存预览");
  });

  it("表单把宽调到当前位置容不下时，位置回退到最后容得下整窗的刻度", async () => {
    // 旧记录（1 毫米）恢复出贴右沿的窄窗 (987,600) 1×88，再切换到 5 毫米
    vi.mocked(fetchLayout).mockResolvedValue({
      verdict: "可裁切",
      grid_step: 1,
      windows: [{ id: 81, position: 0, x: 987, y: 600, w: 1, h: 85 }],
      result: { verdict: "可裁切", ...cleanResult },
      created_at: "2026-09-14T00:00:00Z",
    });
    render(<App />);
    await screen.findByText(/#1 \(987, 600\) 1×85/);
    fireEvent.click(screen.getByTestId("step-btn-5"));
    fireEvent.click(screen.getByText(/#1 \(987, 600\) 1×85/));

    // 宽改 5：原位置 987 放不下（987+5>988），位置回退到 982（982+5=987≤988）
    const wInput = screen.getByLabelText("宽（毫米）");
    fireEvent.change(wInput, { target: { value: "5" } });
    fireEvent.blur(wInput);
    expect(await screen.findByText(/#1 \(982, 600\) 5×85/)).toBeInTheDocument();
  });

  it("后端按刻度打回时保留草稿、步长选择与即时裁决，修正后可直接重试", async () => {
    render(<App />);
    await screen.findByText(/提交方案并裁决/);
    await act(async () => {}); // 等初始 GET 的 Promise 链与 state 更新全部落定
    fireEvent.click(screen.getByTestId("step-btn-5"));
    const svg = document.querySelector(".sheet");
    dragDraw(svg, 217, 302, 262, 337);
    await screen.findByText(/#1 \(217, 302\) 45×35/);

    vi.mocked(submitLayout).mockResolvedValueOnce({
      ok: false,
      status: 422,
      detail: "存在非法开窗，本次提交未保存",
      fieldErrors: [
        { index: 0, fields: { x: "x 必须对齐 5 毫米刻度（以安全内区左上角为基准）" } },
      ],
    });
    fireEvent.click(screen.getByTestId("submit-btn"));
    expect(await screen.findByTestId("submit-errors")).toBeInTheDocument();
    // 草稿、5 毫米选择项、即时裁决全部保留
    expect(screen.getByText(/#1 \(217, 302\) 45×35/)).toBeInTheDocument();
    expect(screen.getByTestId("step-btn-5")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("verdict-banner").textContent).toContain("未保存预览");

    // 修正（任意编辑清掉服务器错误）后重试成功
    fireEvent.click(screen.getByText(/#1 \(217, 302\) 45×35/));
    const wInput = screen.getByLabelText("宽（毫米）");
    fireEvent.change(wInput, { target: { value: "50" } });
    fireEvent.blur(wInput);
    await screen.findByText(/#1 \(217, 302\) 50×35/);

    vi.mocked(submitLayout).mockResolvedValueOnce({
      ok: true,
      data: {
        verdict: "可裁切",
        grid_step: 5,
        windows: [{ id: 71, position: 0, x: 217, y: 302, w: 50, h: 35 }],
        result: { verdict: "可裁切", ...cleanResult },
        created_at: "2026-09-14T00:00:00Z",
      },
    });
    fireEvent.click(screen.getByTestId("submit-btn"));
    await waitFor(() =>
      expect(screen.getByTestId("verdict-banner").textContent).toContain("已保存")
    );
  });
});
