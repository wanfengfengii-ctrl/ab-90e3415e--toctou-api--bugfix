import { describe, expect, it } from "vitest";
import {
  DEFAULT_GRID_STEP,
  DEFECTS,
  GRID_STEPS,
  INNER_BOTTOM,
  INNER_RIGHT,
  MARGIN,
  MAX_LABEL_LENGTH,
  adjudicate,
  fieldErrors,
  gridFieldErrors,
  lastPositionTick,
  normalizeGridStep,
  rectsOverlap,
  snapEdge,
  snapPosition,
  snapSize,
  windowName,
} from "../geometry.js";

describe("半开矩形相交", () => {
  it("正面积相交", () => {
    expect(rectsOverlap({ x: 0, y: 0, w: 10, h: 10 }, { x: 5, y: 5, w: 10, h: 10 })).toBe(true);
  });

  it("边线相接允许", () => {
    expect(rectsOverlap({ x: 0, y: 0, w: 10, h: 10 }, { x: 10, y: 0, w: 10, h: 10 })).toBe(false);
    expect(rectsOverlap({ x: 0, y: 0, w: 10, h: 10 }, { x: 0, y: 10, w: 10, h: 10 })).toBe(false);
  });

  it("角点相接允许", () => {
    expect(rectsOverlap({ x: 0, y: 0, w: 10, h: 10 }, { x: 10, y: 10, w: 10, h: 10 })).toBe(false);
  });

  it("侵入 1mm 即冲突", () => {
    expect(rectsOverlap({ x: 0, y: 0, w: 11, h: 11 }, { x: 10, y: 10, w: 5, h: 5 })).toBe(true);
  });
});

describe("逐字段校验", () => {
  it("边界值合法", () => {
    expect(fieldErrors(MARGIN, MARGIN, INNER_RIGHT - MARGIN, 10)).toEqual({});
  });

  it.each([
    [11, 50, 10, 10, ["x"]],
    [50, 50, 0, 10, ["w"]],
    [50, 50, 10, -1, ["h"]],
    [979, 50, 10, 10, ["x"]],
    [50, 679, 10, 10, ["y"]],
    ["50", 50, 10, 10, ["x"]],
    [3.5, 50, 10, 10, ["x"]],
    [true, 50, 10, 10, ["x"]],
  ])("(%s,%s,%s,%s) 非法字段 %s", (x, y, w, h, keys) => {
    expect(Object.keys(fieldErrors(x, y, w, h)).sort()).toEqual(keys.sort());
  });
});

describe("完整裁决", () => {
  it("干净方案可裁切", () => {
    const out = adjudicate([{ id: 1, x: 300, y: 300, w: 50, h: 50 }]);
    expect(out.verdict).toBe("可裁切");
  });

  it("开窗压瑕疵区 → 不可裁切并给出瑕疵索引", () => {
    const out = adjudicate([{ id: 1, x: 270, y: 180, w: 20, h: 20 }]);
    expect(out.verdict).toBe("不可裁切");
    expect(out.defect_conflicts).toEqual([{ window_id: 1, defect_index: 0 }]);
    expect(out.conflicting_window_ids).toEqual([1]);
  });

  it("开窗边线贴瑕疵区 → 可裁切", () => {
    const out = adjudicate([{ id: 1, x: 280, y: 150, w: 10, h: 10 }]);
    expect(out.verdict).toBe("可裁切");
  });

  it("两个开窗重叠 → 双方高亮", () => {
    const out = adjudicate([
      { id: 1, x: 300, y: 300, w: 50, h: 50 },
      { id: 2, x: 340, y: 340, w: 50, h: 50 },
    ]);
    expect(out.verdict).toBe("不可裁切");
    expect(out.window_conflicts).toEqual([{ window_a: 1, window_b: 2 }]);
    expect(out.conflicting_window_ids).toEqual([1, 2]);
  });

  it("内置瑕疵区坐标只读固定", () => {
    expect(INNER_BOTTOM).toBe(688);
    expect(DEFECTS).toEqual([
      { x: 200, y: 150, w: 80, h: 40 },
      { x: 620, y: 420, w: 60, h: 90 },
    ]);
  });
});

describe("开窗展示名（工件编号或顺序号）", () => {
  it("编号长度上限与后端一致", () => {
    expect(MAX_LABEL_LENGTH).toBe(24);
  });

  it("填写了编号就用编号（去首尾空格）", () => {
    expect(windowName({ label: "ZW-001" }, 0)).toBe("ZW-001");
    expect(windowName({ label: "  ZW-001  " }, 2)).toBe("ZW-001");
  });

  it("未填写编号时退回顺序号", () => {
    expect(windowName({ label: "" }, 0)).toBe("#1");
    expect(windowName({ label: null }, 1)).toBe("#2");
    expect(windowName({}, 2)).toBe("#3");
    expect(windowName({ label: "   " }, 3)).toBe("#4");
  });
});

describe("定位步长常量", () => {
  it("可选步长为 1/5/10，缺省为 1", () => {
    expect(GRID_STEPS).toEqual([1, 5, 10]);
    expect(DEFAULT_GRID_STEP).toBe(1);
  });

  it("规范化步长：缺省/非法返回 null，合法原样返回", () => {
    expect(normalizeGridStep(5)).toBe(5);
    expect(normalizeGridStep(undefined)).toBeNull();
    expect(normalizeGridStep(2)).toBeNull();
    expect(normalizeGridStep("5")).toBeNull();
  });
});

describe("位置吸附 snapPosition（以安全内区左上角 12 为基准）", () => {
  it("1 毫米步长就是就近取整、靠近右/下侧回退到行程末刻度", () => {
    // span=976-w：w=100 → 行程到 x=888
    expect(snapPosition(300, 1, INNER_RIGHT - MARGIN - 100)).toBe(300);
    expect(snapPosition(300.4, 1, INNER_RIGHT - MARGIN - 100)).toBe(300);
    expect(snapPosition(300.6, 1, INNER_RIGHT - MARGIN - 100)).toBe(301);
    expect(snapPosition(950, 1, INNER_RIGHT - MARGIN - 100)).toBe(888);
    expect(snapPosition(-50, 1, INNER_RIGHT - MARGIN - 100)).toBe(MARGIN);
  });

  it("5 毫米步长下取最近刻度，恰好居中取较小值", () => {
    // 刻度 12,17,22,27,...；w=5 → span=971，最大合法起点 982
    const span = INNER_RIGHT - MARGIN - 5;
    expect(snapPosition(220, 5, span)).toBe(222); // 220 离 222（偏移2）比 217（偏移3）近
    expect(snapPosition(219.5, 5, span)).toBe(217); // 正好居中（19.5）→ 取较小刻度
    expect(snapPosition(12, 5, span)).toBe(12);
    expect(snapPosition(900, 5, span)).toBe(902); // 就近吸附，不触发回退
    // 靠近右侧：最后容得下整窗的刻度是 982（982+5=987≤988）
    expect(snapPosition(985, 5, span)).toBe(982);
    expect(snapPosition(987, 5, span)).toBe(982);
    expect(snapPosition(999, 5, span)).toBe(982);
  });

  it("10 毫米步长下靠近下侧时回退到最后容得下整窗的刻度", () => {
    // 刻度 12,22,32,...；h=10 → span=666，最后起点 672（672+10=682）
    const span = INNER_BOTTOM - MARGIN - 10;
    expect(snapPosition(303, 10, span)).toBe(302);
    expect(snapPosition(308, 10, span)).toBe(312);
    expect(snapPosition(677, 10, span)).toBe(672); // 下一刻度 682 会让整窗越过 688
    expect(snapPosition(690, 10, span)).toBe(672);
  });
});

describe("尺寸吸附 snapSize", () => {
  it("取最近倍数，恰好居中取较小值", () => {
    expect(snapSize(10, 5)).toBe(10);
    expect(snapSize(12, 5)).toBe(10); // 距 10 为 2、距 15 为 3 → 10
    expect(snapSize(13, 5)).toBe(15);
    expect(snapSize(2.5, 5)).toBe(5); // 0 与 5 居中取较小的正值 5
    expect(snapSize(0.4, 5)).toBe(5); // 小于半步仍保证最小一格
    expect(snapSize(0, 10)).toBe(10);
  });

  it("给定可容纳上限时回退到容得下的最大倍数", () => {
    // 常态：位置在刻度上时 maxFit 必为步长整数倍
    expect(snapSize(10, 5, 6)).toBe(5);  // 10 放不进剩余 6mm，回退一格
    expect(snapSize(20, 10, 16)).toBe(10); // 20 放不进剩余 16mm，回退到 10
  });
});

describe("自由边吸附 snapEdge", () => {
  it("吸附到 [12,988] 内最近刻度", () => {
    expect(snapEdge(220, 5, INNER_RIGHT)).toBe(222);
    expect(snapEdge(219.5, 5, INNER_RIGHT)).toBe(217);
    expect(snapEdge(986, 5, INNER_RIGHT)).toBe(987);
    expect(snapEdge(988, 5, INNER_RIGHT)).toBe(987);
    expect(snapEdge(1000, 5, INNER_RIGHT)).toBe(987);
    expect(snapEdge(0, 5, INNER_RIGHT)).toBe(12);
  });
});

describe("尺寸变大时位置回退 lastPositionTick", () => {
  it("给出容得下整窗的最后位置刻度", () => {
    // floor((988-12-size)/step)*step + 12
    expect(lastPositionTick(5, 5, INNER_RIGHT)).toBe(982);  // 982+5=987 ≤ 988
    expect(lastPositionTick(1, 5, INNER_RIGHT)).toBe(987);
    expect(lastPositionTick(45, 5, INNER_RIGHT)).toBe(942); // 942+45=987
    expect(lastPositionTick(10, 10, INNER_RIGHT)).toBe(972);
    expect(lastPositionTick(10, 10, INNER_BOTTOM)).toBe(672);
  });
});

describe("刻度对齐校验 gridFieldErrors", () => {
  it("对齐时无错误", () => {
    expect(gridFieldErrors(12, 12, 5, 5, 5)).toEqual({});
    expect(gridFieldErrors(987, 687, 5, 5, 5)).toEqual({});
    expect(gridFieldErrors(982, 682, 10, 10, 10)).toEqual({});
  });

  it.each([
    [13, 12, 5, 5, 5, ["x"]],
    [12, 14, 5, 5, 5, ["y"]],
    [12, 12, 6, 5, 5, ["w"]],
    [12, 12, 5, 4, 5, ["h"]],
    [14, 16, 7, 3, 5, ["x", "y", "w", "h"]],
  ])("(%s,%s,%s,%s) 步长 %s 非法字段 %s", (x, y, w, h, step, keys) => {
    expect(Object.keys(gridFieldErrors(x, y, w, h, step)).sort()).toEqual(keys.sort());
  });
});
