export const MAX_DIFF_CELLS = 4_000_000;

export function diff(before, after) {
  const a = before.split('\n'), b = after.split('\n'), result = [];
  let start = 0, endA = a.length, endB = b.length;
  while (start < endA && start < endB && a[start] === b[start]) {
    result.push(['ctx', a[start++]]);
  }
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
  const n = endA - start, m = endB - start;
  let i = start, j = start;
  if (!n || !m || n * m > MAX_DIFF_CELLS) {
    while (i < endA) result.push(['del', a[i++]]);
    while (j < endB) result.push(['add', b[j++]]);
  } else {
    const width = m + 1, table = new Uint32Array((n + 1) * width);
    for (let x = n - 1; x >= 0; x--) {
      for (let y = m - 1; y >= 0; y--) {
        table[x * width + y] = a[start + x] === b[start + y]
          ? table[(x + 1) * width + y + 1] + 1
          : Math.max(table[(x + 1) * width + y], table[x * width + y + 1]);
      }
    }
    while (i < endA && j < endB) {
      if (a[i] === b[j]) { result.push(['ctx', a[i]]); i++; j++; }
      else if (table[(i - start + 1) * width + j - start] >= table[(i - start) * width + j - start + 1]) result.push(['del', a[i++]]);
      else result.push(['add', b[j++]]);
    }
    while (i < endA) result.push(['del', a[i++]]);
    while (j < endB) result.push(['add', b[j++]]);
  }
  for (let index = endA; index < a.length; index++) result.push(['ctx', a[index]]);
  return result;
}
