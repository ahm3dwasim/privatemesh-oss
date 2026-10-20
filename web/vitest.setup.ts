import "@testing-library/jest-dom/vitest";

// jsdom has no layout engine, so it never implements scrollIntoView — stub it globally rather
// than in each test that renders a component calling it.
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
