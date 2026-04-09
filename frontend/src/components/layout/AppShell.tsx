import { Outlet } from "react-router-dom";
import { NavBar } from "./NavBar";

export function AppShell() {
  return (
    <div className="flex h-[100svh] flex-col overflow-hidden">
      <NavBar />
      <main className="flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}
