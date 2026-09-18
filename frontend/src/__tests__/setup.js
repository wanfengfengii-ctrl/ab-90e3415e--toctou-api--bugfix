import "@testing-library/jest-dom/vitest";

// jsdom 不实现 SVG 几何接口，测试中用单位矩阵兜底：
// 指针事件里的 clientX/clientY 直接等同于纸面毫米坐标。
if (typeof globalThis.DOMMatrix !== "function") {
  class DOMMatrix {
    inverse() {
      return this;
    }
  }
  globalThis.DOMMatrix = DOMMatrix;
}

if (typeof globalThis.DOMPoint !== "function") {
  class DOMPoint {
    constructor(x, y) {
      this.x = x;
      this.y = y;
    }
    matrixTransform() {
      return this;
    }
  }
  globalThis.DOMPoint = DOMPoint;
}

if (typeof SVGElement !== "undefined" && !SVGElement.prototype.getScreenCTM) {
  SVGElement.prototype.getScreenCTM = function getScreenCTM() {
    return new globalThis.DOMMatrix();
  };
}
