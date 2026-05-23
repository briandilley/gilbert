import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addMinutes,
  dateInputValue,
  defaultEventTimesForDate,
  localDateKey,
  localDateTimeInputValue,
} from "./datetime.ts";

test("localDateKey groups by the user's local day", () => {
  const date = new Date(2026, 5, 1, 23, 30);

  assert.equal(localDateKey(date), "2026-06-01");
});

test("defaultEventTimesForDate uses selected future day at 9 AM for one hour", () => {
  const selectedDay = new Date(2026, 5, 5, 0, 0);
  const now = new Date(2026, 5, 1, 13, 12);

  assert.deepEqual(defaultEventTimesForDate(selectedDay, now), {
    start: "2026-06-05T09:00",
    end: "2026-06-05T10:00",
  });
});

test("defaultEventTimesForDate rounds today's next start to the next half hour", () => {
  const selectedDay = new Date(2026, 5, 1, 0, 0);
  const now = new Date(2026, 5, 1, 13, 12);

  assert.deepEqual(defaultEventTimesForDate(selectedDay, now), {
    start: "2026-06-01T13:30",
    end: "2026-06-01T14:30",
  });
});

test("date and datetime input formatting is local and zero-padded", () => {
  const date = new Date(2026, 0, 2, 3, 4);

  assert.equal(dateInputValue(date), "2026-01-02");
  assert.equal(localDateTimeInputValue(date), "2026-01-02T03:04");
});

test("addMinutes preserves local input format", () => {
  assert.equal(addMinutes("2026-06-01T09:45", 30), "2026-06-01T10:15");
});
