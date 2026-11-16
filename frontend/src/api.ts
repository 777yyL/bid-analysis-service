const KEY = 'bid-analysis-api-key';
export const savedKey = () => sessionStorage.getItem(KEY) || '';
export const saveKey = (key: string) => key ? sessionStorage.setItem(KEY, key) : sessionStorage.removeItem(KEY);
export async function api<T>(path: string, options: RequestInit = {}, direct = false): Promise<T> {
  const response = await fetch(`${direct ? '' : '/api/v3'}${path}`, {
    ...options,
    headers: {'Content-Type': 'application/json', 'X-API-Key': savedKey(), ...options.headers}
  });
  if (!response.ok) {
    let detail: unknown;
    try {detail = (await response.json()).detail;} catch {detail = response.statusText;}
    throw new Error(typeof detail === 'string' ? detail : `请求失败 (${response.status})：${JSON.stringify(detail || '')}`);
  }
  return response.status === 204 ? undefined as T : response.json();
}
export const post = <T,>(path: string, body: unknown = {}, direct = false) => api<T>(path, {method: 'POST', body: JSON.stringify(body)}, direct);
export function query(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {if (value !== undefined && value !== '') params.set(key, String(value));});
  return params.toString() ? `?${params}` : '';
}
export function exportCsv(rows: Record<string, unknown>[], name: string) {
  if (!rows.length) return;
  const keys = Object.keys(rows[0]);
  const escape = (value: unknown) => {
    let text = String(value ?? '');
    if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  const data = [keys, ...rows.map(row => keys.map(key => row[key]))].map(row => row.map(escape).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob(['\uFEFF', data], {type: 'text/csv;charset=utf-8'}));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click(); URL.revokeObjectURL(url);
}
