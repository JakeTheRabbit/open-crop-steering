/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export — served by FastAPI at / under HA Ingress
  output: 'export',
  // HA Ingress URL has dynamic base path; set basePath via env at build time if needed
  // basePath: process.env.NEXT_PUBLIC_BASE_PATH || '',
  trailingSlash: true,
  images: {
    // Static export can't use Next image optimization
    unoptimized: true,
  },
  // No app/api routes — backend is FastAPI. Linter rule below also enforces.
  experimental: {
    // (Reserved for future flags)
  },
};

module.exports = nextConfig;
