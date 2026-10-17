/** Synthetic/local-demo uploads only. Isolated from real API storage and credentials. */
const DATABASE = 'huipi-demo-files-v2'
async function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1)
    request.onupgradeneeded = () => request.result.createObjectStore('files')
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(new Error('浏览器无法保存演示文件，请检查本地存储权限。'))
  })
}
export async function saveDemoFile(id: string, file: Blob): Promise<void> {
  const db = await database()
  try {
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction('files', 'readwrite')
      tx.objectStore('files').put(file, id)
      tx.oncomplete = () => resolve()
      tx.onerror = () => reject(new Error('演示文件未保存，请检查浏览器空间。'))
      tx.onabort = () => reject(new Error('演示文件保存已中断。'))
    })
  } finally {
    db.close()
  }
}
export async function loadDemoFile(id: string): Promise<Blob> {
  const db = await database()
  try {
    return await new Promise<Blob>((resolve, reject) => {
      const request = db.transaction('files').objectStore('files').get(id)
      request.onsuccess = () =>
        request.result instanceof Blob
          ? resolve(request.result)
          : reject(new Error('本地演示文件不可用，请重新上传；不会以合成试卷代替原文件。'))
      request.onerror = () => reject(new Error('无法读取演示文件。'))
    })
  } finally {
    db.close()
  }
}
