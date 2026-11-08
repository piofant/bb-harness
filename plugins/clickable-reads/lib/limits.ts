/** Largest PNG (as base64) the Mac clipboard route takes: bb reads raster
 * images up to 10 MB, and PNG re-encoding can grow them a bit. */
export const MAX_CLIPBOARD_PNG_BASE64 = 24 * 1024 * 1024;
