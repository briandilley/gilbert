function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

export function dateInputValue(date: Date): string {
  return [
    date.getFullYear(),
    pad2(date.getMonth() + 1),
    pad2(date.getDate()),
  ].join("-");
}

export function localDateKey(value: Date | string): string {
  const date = value instanceof Date ? value : new Date(value);
  return dateInputValue(date);
}

export function localDateTimeInputValue(date: Date): string {
  return `${dateInputValue(date)}T${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

export function addMinutes(localDateTime: string, minutes: number): string {
  const date = new Date(localDateTime);
  date.setMinutes(date.getMinutes() + minutes);
  return localDateTimeInputValue(date);
}

export function defaultEventTimesForDate(
  selectedDate: Date | null,
  now = new Date(),
): { start: string; end: string } {
  const base = selectedDate ? new Date(selectedDate) : new Date(now);
  const todayKey = dateInputValue(now);
  const selectedKey = dateInputValue(base);

  if (selectedKey === todayKey) {
    base.setHours(now.getHours(), now.getMinutes(), 0, 0);
    const roundedMinutes = Math.ceil(base.getMinutes() / 30) * 30;
    base.setMinutes(roundedMinutes, 0, 0);
  } else {
    base.setHours(9, 0, 0, 0);
  }

  const end = new Date(base);
  end.setHours(end.getHours() + 1);
  return {
    start: localDateTimeInputValue(base),
    end: localDateTimeInputValue(end),
  };
}
