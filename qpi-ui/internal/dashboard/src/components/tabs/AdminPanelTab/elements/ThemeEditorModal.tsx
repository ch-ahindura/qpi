import { useState, useEffect } from "react";
import { pb } from "@/lib/pb";
import type { ThemeRecord } from "@/types";
import { applyTokens, useTheme } from "@/lib/ThemeContext";

interface Props {
  theme: ThemeRecord | null;
  onClose: (refresh: boolean) => void;
}

export function ThemeEditorModal({ theme, onClose }: Props) {
  const [name, setName] = useState(theme?.name || "");
  const [siteName, setSiteName] = useState(theme?.site_name || "");
  const [tagline, setTagline] = useState(theme?.tagline || "");
  const [tokensStr, setTokensStr] = useState(
    theme?.tokens ? JSON.stringify(theme.tokens, null, 2) : "",
  );
  const [customCSS, setCustomCSS] = useState(theme?.custom_css || "");
  const [customJS, setCustomJS] = useState(theme?.custom_js || "");
  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [faviconFile, setFaviconFile] = useState<File | null>(null);
  const [saving, setSaving] = useState(false);

  const { theme: activeTheme, isDark } = useTheme();

  const handlePreview = () => {
    // 1. Apply Custom CSS
    let previewStyleEl = document.getElementById("qpi-theme-css-preview") as HTMLStyleElement | null;
    if (!previewStyleEl) {
      previewStyleEl = document.createElement("style");
      previewStyleEl.id = "qpi-theme-css-preview";
      document.head.appendChild(previewStyleEl);
    }
    console.log("Setting previewStyleEl.textContent to:", customCSS);
    previewStyleEl.textContent = customCSS;

    // 2. Apply Design Tokens
    if (!tokensStr.trim()) {
      applyTokens(null, isDark);
    } else {
      try {
        const tokensObj = JSON.parse(tokensStr);
        applyTokens(tokensObj, isDark);
      } catch {
        alert("Invalid JSON in Design Tokens. Cannot preview design tokens.");
      }
    }
  };

  const handleResetPreview = () => {
    // 1. Reset Design Tokens
    if (activeTheme?.tokens) {
      applyTokens(activeTheme.tokens, isDark);
    } else {
      applyTokens(null, isDark);
    }

    // 2. Reset Custom CSS
    const previewStyleEl = document.getElementById("qpi-theme-css-preview");
    if (previewStyleEl) {
      previewStyleEl.remove();
    }
  };

  const handleClose = () => {
    handleResetPreview();
    onClose(false);
  };

  useEffect(() => {
    if (!theme) {
      // Fetch defaults
      fetch("/api/theme/defaults")
        .then((res) => res.json())
        .then((data) => {
          if (data.branding) {
            if (!siteName) setSiteName(data.branding.site_name || "");
            if (!tagline) setTagline(data.branding.tagline || "");
          }
          if (data.tokens && !tokensStr) {
            setTokensStr(JSON.stringify(data.tokens, null, 2));
          }
        })
        .catch((err) => console.error("Failed to fetch theme defaults", err));
    }
  }, [theme, siteName, tagline, tokensStr]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);

    try {
      let tokensObj = null;
      if (tokensStr.trim()) {
        try {
          tokensObj = JSON.parse(tokensStr);
        } catch (e) {
          throw new Error("Invalid JSON in Design Tokens", { cause: e });
        }
      }

      const formData = new FormData();
      formData.append("name", name);
      formData.append("site_name", siteName);
      formData.append("tagline", tagline);
      formData.append("custom_css", customCSS);
      formData.append("custom_js", customJS);
      if (tokensObj) {
        formData.append("tokens", JSON.stringify(tokensObj));
      }
      if (logoFile) {
        formData.append("logo", logoFile);
      }
      if (faviconFile) {
        formData.append("favicon", faviconFile);
      }

      if (theme) {
        await pb.collection("themes").update(theme.id, formData);
      } else {
        await pb.collection("themes").create(formData);
      }

      onClose(true);
    } catch (err: unknown) {
      alert(`Save failed: ${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
      <div className="bg-white dark:bg-zinc-950 border border-gray-200 dark:border-zinc-800 rounded-lg shadow-xl w-full max-w-4xl max-h-[90vh] flex flex-col">
        <div className="flex justify-between items-center p-4 border-b border-gray-200 dark:border-zinc-800">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white font-geist">
            {theme ? "Edit Theme" : "Create New Theme"}
          </h2>
          <button
            onClick={handleClose}
            className="text-gray-500 hover:text-gray-700 dark:text-zinc-400 dark:hover:text-zinc-200"
          >
            ✕
          </button>
        </div>

        <div className="overflow-y-auto p-4 flex-1">
          <form
            id="theme-form"
            onSubmit={handleSubmit}
            className="space-y-6 text-sm"
          >
            <div className="grid grid-cols-2 gap-6">
              <div>
                <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
                  Theme Name
                </label>
                <input
                  type="text"
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="w-full bg-gray-50 dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded px-3 py-2 text-gray-900 dark:text-white focus:outline-none focus:border-zinc-500 transition-colors"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
                    Site Name
                  </label>
                  <input
                    type="text"
                    value={siteName}
                    onChange={(e) => setSiteName(e.target.value)}
                    className="w-full bg-gray-50 dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded px-3 py-2 text-gray-900 dark:text-white focus:outline-none focus:border-zinc-500 transition-colors"
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
                    Tagline
                  </label>
                  <input
                    type="text"
                    value={tagline}
                    onChange={(e) => setTagline(e.target.value)}
                    className="w-full bg-gray-50 dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded px-3 py-2 text-gray-900 dark:text-white focus:outline-none focus:border-zinc-500 transition-colors"
                  />
                </div>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-6">
              <div>
                <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
                  Logo Image (optional)
                </label>
                <input
                  type="file"
                  accept="image/png,image/svg+xml,image/webp"
                  onChange={(e) => setLogoFile(e.target.files?.[0] || null)}
                  className="block w-full text-sm text-gray-500 dark:text-zinc-400 file:mr-4 file:py-2 file:px-4 file:rounded file:border-0 file:text-sm file:font-semibold file:bg-gray-100 dark:file:bg-zinc-800 file:text-gray-700 dark:file:text-zinc-300 hover:file:bg-gray-200 dark:hover:file:bg-zinc-700 transition-colors cursor-pointer"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
                  Favicon (optional)
                </label>
                <input
                  type="file"
                  accept="image/png,image/svg+xml,image/x-icon,image/webp"
                  onChange={(e) => setFaviconFile(e.target.files?.[0] || null)}
                  className="block w-full text-sm text-gray-500 dark:text-zinc-400 file:mr-4 file:py-2 file:px-4 file:rounded file:border-0 file:text-sm file:font-semibold file:bg-gray-100 dark:file:bg-zinc-800 file:text-gray-700 dark:file:text-zinc-300 hover:file:bg-gray-200 dark:hover:file:bg-zinc-700 transition-colors cursor-pointer"
                />
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
                Design Tokens (JSON)
              </label>
              <textarea
                value={tokensStr}
                onChange={(e) => setTokensStr(e.target.value)}
                className="w-full bg-gray-50 dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded px-3 py-2 text-gray-900 dark:text-white focus:outline-none focus:border-zinc-500 transition-colors h-48 font-mono text-xs"
                placeholder="{}"
              />
            </div>

            <div className="grid grid-cols-2 gap-6">
              <div>
                <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider">
                  Custom CSS
                </label>
                <textarea
                  value={customCSS}
                  onChange={(e) => setCustomCSS(e.target.value)}
                  className="w-full bg-gray-50 dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded px-3 py-2 text-gray-900 dark:text-white focus:outline-none focus:border-zinc-500 transition-colors h-32 font-mono text-xs"
                  placeholder="body { ... }"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-500 dark:text-zinc-400 mb-1.5 uppercase tracking-wider flex justify-between items-center">
                  <span>Custom JS</span>
                  <span className="text-yellow-600 dark:text-yellow-500 text-[10px] bg-yellow-50 dark:bg-yellow-900/30 px-2 py-0.5 rounded">
                    ⚠️ Runs with full page access
                  </span>
                </label>
                <textarea
                  value={customJS}
                  onChange={(e) => setCustomJS(e.target.value)}
                  className="w-full bg-gray-50 dark:bg-zinc-900 border border-gray-200 dark:border-zinc-800 rounded px-3 py-2 text-gray-900 dark:text-white focus:outline-none focus:border-zinc-500 transition-colors h-32 font-mono text-xs"
                  placeholder="console.log('hello');"
                />
              </div>
            </div>
          </form>
        </div>

        <div className="p-4 border-t border-gray-200 dark:border-zinc-800 flex justify-between items-center">
          <div className="space-x-3">
            <button
              type="button"
              onClick={handlePreview}
              className="px-4 py-2 text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-900/30 rounded transition-colors"
            >
              Preview
            </button>
            <button
              type="button"
              onClick={handleResetPreview}
              className="px-4 py-2 text-gray-600 dark:text-zinc-400 hover:bg-gray-100 dark:hover:bg-zinc-800 rounded transition-colors text-sm"
            >
              Reset Preview
            </button>
          </div>
          <div className="space-x-3 flex">
            <button
              type="button"
              onClick={handleClose}
              className="px-4 py-2 text-gray-600 dark:text-zinc-300 hover:bg-gray-100 dark:hover:bg-zinc-800 rounded transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              form="theme-form"
              disabled={saving}
              className="bg-white text-zinc-950 font-semibold py-2 px-6 rounded hover:opacity-90 transition-opacity focus:outline-none disabled:opacity-50"
            >
              {saving ? "Saving..." : "Save Theme"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
