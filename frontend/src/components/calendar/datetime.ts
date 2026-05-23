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

export interface CalendarDay {
  date: Date;
  dateKey: string;
  dayOfMonth: number;
  isCurrentMonth: boolean;
}

export function buildMonthCalendar(monthDate: Date): CalendarDay[][] {
  const firstOfMonth = new Date(
    monthDate.getFullYear(),
    monthDate.getMonth(),
    1,
  );
  const cursor = new Date(firstOfMonth);
  cursor.setDate(cursor.getDate() - cursor.getDay());

  const weeks: CalendarDay[][] = [];
  for (let weekIndex = 0; weekIndex < 6; weekIndex += 1) {
    const week: CalendarDay[] = [];
    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const date = new Date(cursor);
      week.push({
        date,
        dateKey: dateInputValue(date),
        dayOfMonth: date.getDate(),
        isCurrentMonth: date.getMonth() === monthDate.getMonth(),
      });
      cursor.setDate(cursor.getDate() + 1);
    }
    weeks.push(week);
  }
  return weeks;
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
