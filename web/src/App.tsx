import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/layout";

const RegisterPage = lazy(() =>
  import("@/pages/RegisterPage").then((module) => ({ default: module.RegisterPage })),
);
const PoolPage = lazy(() =>
  import("@/pages/PoolPage").then((module) => ({ default: module.PoolPage })),
);

export default function App() {
  return (
    <Suspense fallback={null}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/register" replace />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/pool" element={<PoolPage />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
