import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Do not generate AGENTS.md / CLAUDE.md when `next dev` detects a coding agent.
  agentRules: false,
};

export default nextConfig;
