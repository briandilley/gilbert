import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/hooks/useAuth";
import { fetchLoginMethods, loginLocal } from "@/api/auth";
import { fetchScreensInfo } from "@/api/screens";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";

export function LoginPage() {
  const navigate = useNavigate();
  const { refresh } = useAuth();
  const [searchParams] = useSearchParams();
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(searchParams.get("error") || "");
  const [loading, setLoading] = useState(false);

  const { data: methods } = useQuery({
    queryKey: ["auth-methods"],
    queryFn: fetchLoginMethods,
  });

  const { data: screensInfo } = useQuery({
    queryKey: ["screens-info"],
    queryFn: fetchScreensInfo,
  });

  const formMethods = methods?.filter((m) => m.method === "form") ?? [];
  const redirectMethods = methods?.filter((m) => m.method === "redirect") ?? [];

  async function handleLocalLogin(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await loginLocal(identifier, password);
      await refresh();
      navigate("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle className="text-2xl">Gilbert</CardTitle>
          <CardDescription>Sign in to continue</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {error && (
            <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          {formMethods.length > 0 && (
            <form onSubmit={handleLocalLogin} className="space-y-3">
              <div className="space-y-1.5">
                <Label htmlFor="identifier">Email or username</Label>
                <Input
                  id="identifier"
                  type="text"
                  autoComplete="username"
                  value={identifier}
                  onChange={(e) => setIdentifier(e.target.value)}
                  placeholder="you@example.com or username"
                  required
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="password">Password</Label>
                <Input
                  id="password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </div>
              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? "Signing in..." : "Sign in"}
              </Button>
            </form>
          )}

          {formMethods.length > 0 && redirectMethods.length > 0 && (
            <div className="relative">
              <Separator />
              <span className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 bg-card px-2 text-xs text-muted-foreground">
                or
              </span>
            </div>
          )}

          {redirectMethods.map((method) => (
            <Button
              key={method.provider_type}
              variant="outline"
              className="w-full"
              render={<a href={method.redirect_url} />}
            >
              {method.display_name}
            </Button>
          ))}

          {screensInfo?.enabled && screensInfo.allow_guest_screens && (
            <Button
              variant="outline"
              className="w-full"
              onClick={() => navigate("/screens")}
            >
              Set up a screen
            </Button>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
