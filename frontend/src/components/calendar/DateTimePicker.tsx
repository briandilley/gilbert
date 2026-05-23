import { useMemo, useState } from "react";
import { Popover as PopoverPrimitive } from "@base-ui/react/popover";
import { CalendarIcon, ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { buildMonthCalendar, dateInputValue } from "./datetime";

interface Props {
  label: string;
  date: string;
  time: string;
  onDateChange: (date: string) => void;
  onTimeChange: (time: string) => void;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function DateTimePicker({
  label,
  date,
  time,
  onDateChange,
  onTimeChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const [visibleMonth, setVisibleMonth] = useState(() => parseDateKey(date));
  const weeks = useMemo(() => buildMonthCalendar(visibleMonth), [visibleMonth]);
  const todayKey = dateInputValue(new Date());

  const moveMonth = (delta: number) => {
    setVisibleMonth(
      (current) => new Date(current.getFullYear(), current.getMonth() + delta, 1),
    );
  };

  const selectDate = (dateKey: string) => {
    onDateChange(dateKey);
    setVisibleMonth(parseDateKey(dateKey));
    setOpen(false);
  };

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_7.5rem] gap-2">
      <div className="space-y-2">
        <Label>{label} date</Label>
        <PopoverPrimitive.Root open={open} onOpenChange={setOpen}>
          <PopoverPrimitive.Trigger
            render={
              <Button
                type="button"
                variant="outline"
                className="h-7 w-full justify-start px-2.5 font-normal"
                aria-label={`Pick ${label.toLowerCase()} date`}
              />
            }
          >
            <CalendarIcon className="size-3.5 text-muted-foreground" />
            <span>{formatDateLabel(date)}</span>
          </PopoverPrimitive.Trigger>
          <PopoverPrimitive.Portal>
            <PopoverPrimitive.Positioner
              className="isolate z-50 outline-none"
              align="start"
              sideOffset={6}
            >
              <PopoverPrimitive.Popup
                className={cn(
                  "w-72 rounded-md border border-border bg-popover p-3 text-popover-foreground",
                  "shadow-[0_4px_16px_-4px_rgb(0_0_0_/_0.35)]",
                  "origin-(--transform-origin) outline-none",
                  "data-open:animate-in data-open:fade-in-0 data-open:zoom-in-[0.98]",
                  "data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-[0.98]",
                  "duration-100",
                )}
              >
                <div className="mb-2 flex items-center justify-between">
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => moveMonth(-1)}
                    aria-label="Previous month"
                  >
                    <ChevronLeft className="size-3.5" />
                  </Button>
                  <div className="text-sm font-medium">
                    {visibleMonth.toLocaleDateString(undefined, {
                      month: "long",
                      year: "numeric",
                    })}
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => moveMonth(1)}
                    aria-label="Next month"
                  >
                    <ChevronRight className="size-3.5" />
                  </Button>
                </div>
                <div className="grid grid-cols-7 gap-1 text-center text-[11px] text-muted-foreground">
                  {WEEKDAYS.map((weekday) => (
                    <div key={weekday} className="h-6 leading-6">
                      {weekday}
                    </div>
                  ))}
                </div>
                <div className="grid grid-cols-7 gap-1">
                  {weeks.flat().map((day) => {
                    const selected = day.dateKey === date;
                    const today = day.dateKey === todayKey;
                    return (
                      <button
                        key={day.dateKey}
                        type="button"
                        onClick={() => selectDate(day.dateKey)}
                        className={cn(
                          "h-8 rounded-md text-sm outline-none",
                          "transition-[background-color,color,opacity] duration-(--duration-fast) ease-(--ease-out)",
                          "focus-visible:outline-2 focus-visible:outline-(--signal) focus-visible:outline-offset-1",
                          selected
                            ? "bg-foreground text-background"
                            : "text-foreground hover:bg-foreground/8",
                          !selected &&
                            today &&
                            "border border-(--signal)/50 text-(--signal)",
                          !day.isCurrentMonth && "text-muted-foreground opacity-60",
                        )}
                      >
                        {day.dayOfMonth}
                      </button>
                    );
                  })}
                </div>
              </PopoverPrimitive.Popup>
            </PopoverPrimitive.Positioner>
          </PopoverPrimitive.Portal>
        </PopoverPrimitive.Root>
      </div>
      <div className="space-y-2">
        <Label>{label} time</Label>
        <Input
          type="time"
          step={900}
          value={time}
          onChange={(e) => onTimeChange(e.target.value)}
        />
      </div>
    </div>
  );
}

function parseDateKey(dateKey: string): Date {
  const [year, month, day] = dateKey.split("-").map(Number);
  return new Date(year, month - 1, day || 1);
}

function formatDateLabel(dateKey: string): string {
  return parseDateKey(dateKey).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}
