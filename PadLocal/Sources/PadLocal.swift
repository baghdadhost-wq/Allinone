import SwiftUI
import CryptoKit
import UniformTypeIdentifiers

struct Entry: Identifiable, Codable {
    let name: String
    let repo: String
    let file: String
    let estimate: Int64
    var id: String { repo.hasPrefix("file:") ? repo : repo + "/" + file }
    var localName: String {
        if Self.choices.contains(where: { $0.id == id }) { return file }
        let prefix = SHA256.hash(data: Data(id.utf8)).prefix(12).map { String(format:"%02x",$0) }.joined()
        return prefix + "-" + String((file as NSString).lastPathComponent.prefix(80)) + ".gguf"
    }
    static let choices = [
        Entry(name:"Qwen 3.5 4B · Abliterated",repo:"privastudio/qwen3.5-abliterated",file:"Huihui-Qwen3.5-4B-abliterated.Q4_K_M.gguf",estimate:2_710_000_000),
        Entry(name:"Gemma 4 E4B · Heretic",repo:"spale04/gemma-4-E4B-it-heretic-GGUF",file:"gemma-4-E4B-it-heretic-Q4_K_M-imat.gguf",estimate:5_300_000_000),
        Entry(name:"Bonsai 2 27B · Heretic",repo:"OS-Software/Ternary-Bonsai-2-27B-Uncensored-Heretic-GGUF",file:"Ternary-Bonsai-2-27B-Uncensored-Heretic-PTQ1_0.gguf",estimate:5_950_000_000)
    ]
}
struct Issue: LocalizedError {
    let text: String
    init(_ s:String){text=s}
    var errorDescription:String? {text}
}
func sizeText(_ bytes:Int64)->String { ByteCountFormatter.string(fromByteCount:bytes,countStyle:.decimal) }
let files = FileManager.default
let models = files.urls(for:.applicationSupportDirectory,in:.userDomainMask)[0].appendingPathComponent("Models",isDirectory:true)
func disk(_ folder:URL)->Int64 { (try? folder.resourceValues(forKeys:[.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage) ?? 0 }

struct Card: Decodable {
    struct Sibling: Decodable {
        struct Blob: Decodable { let sha256:String; let size:Int64 }
        let rfilename:String
        let lfs:Blob?
    }
    let sha:String
    let siblings:[Sibling]
}
private final class TransferMeter: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    let report: @Sendable (Int64) -> Void
    private var lastUpdate = -Double.infinity
    init(report: @escaping @Sendable (Int64) -> Void) { self.report = report }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didWriteData bytesWritten: Int64, totalBytesWritten: Int64,
                    totalBytesExpectedToWrite: Int64) {
        let now = ProcessInfo.processInfo.systemUptime
        if now - lastUpdate >= 0.25 || totalBytesWritten == totalBytesExpectedToWrite {
            lastUpdate = now
            report(totalBytesWritten)
        }
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didFinishDownloadingTo location: URL) {}
}

func download(_ entry:Entry,to folder:URL,progress:@escaping @Sendable (Int64, Int64)->Void,status:@escaping @MainActor (String)->Void) async throws {
    let api=URL(string:"https://huggingface.co/api/models/"+entry.repo+"?blobs=true")!
    let (metadata,response)=try await URLSession.shared.data(from:api)
    guard (response as? HTTPURLResponse)?.statusCode == 200 else {throw Issue("تعذرت قراءة معلومات النموذج.")}
    let card=try JSONDecoder().decode(Card.self,from:metadata)
    guard let blob=card.siblings.first(where:{$0.rfilename==entry.file})?.lfs, blob.size>0,
          blob.sha256.count==64,card.sha.count==40 else {throw Issue("تعذر التحقق من بصمة الملف.")}
    guard disk(folder)>blob.size+300_000_000 else {throw Issue("مساحة التخزين المتاحة غير كافية.")}
    progress(0, blob.size)
    await status("تنزيل "+sizeText(blob.size)+"…")
    let url=URL(string:"https://huggingface.co")!.appendingPathComponent(entry.repo).appendingPathComponent("resolve").appendingPathComponent(card.sha).appendingPathComponent(entry.file)
    let meter = TransferMeter { progress($0, blob.size) }
    let (temp,reply)=try await URLSession.shared.download(from:url, delegate:meter)
    defer {try? files.removeItem(at:temp)}
    guard (reply as? HTTPURLResponse)?.statusCode==200 else {throw Issue("فشل تنزيل الأوزان.")}
    progress(blob.size, blob.size)
    await status("التحقق من سلامة الأوزان…")
    let handle=try FileHandle(forReadingFrom:temp)
    defer {try? handle.close()}
    var hash=SHA256(),length:Int64=0
    while let data=try handle.read(upToCount:1<<20), !data.isEmpty {
        try Task.checkCancellation()
        hash.update(data:data);length+=Int64(data.count)
    }
    guard length==blob.size,hash.finalize().map({String(format:"%02x",$0)}).joined()==blob.sha256.lowercased() else {throw Issue("بصمة الملف لا تطابق النسخة المنشورة.")}
    try Task.checkCancellation()
    let destination = folder.appendingPathComponent(entry.localName)
    let sidecar = destination.appendingPathExtension("jinja")
    let companion = entry.file.split(separator:"/").dropLast().map(String.init) + ["chat_template.jinja"]
    let templatePath = [companion.joined(separator:"/"), "chat_template.jinja"].first { path in
        card.siblings.contains { $0.rfilename == path }
    }
    var installedTemplate = false
    if let templatePath, !files.fileExists(atPath:sidecar.path) {
        await status("تجهيز قالب المحادثة المرافق…")
        let revision = URL(string:"https://huggingface.co")!.appendingPathComponent(entry.repo)
            .appendingPathComponent("resolve").appendingPathComponent(card.sha)
        do {
            let (stream, response) = try await URLSession.shared.bytes(from:revision.appendingPathComponent(templatePath))
            if (response as? HTTPURLResponse)?.statusCode == 200 {
                var bytes = Data()
                for try await byte in stream {
                    if bytes.count >= 512 * 1024 { throw Issue("قالب المحادثة كبير جدا.") }
                    bytes.append(byte)
                }
                if !bytes.isEmpty, String(data:bytes,encoding:.utf8) != nil {
                    try bytes.write(to:sidecar,options:.withoutOverwriting)
                    installedTemplate = true
                }
            }
        } catch { try Task.checkCancellation() }
    }
    do {
        try Task.checkCancellation()
        try files.moveItem(at:temp,to:destination)
    } catch {
        if installedTemplate { try? files.removeItem(at:sidecar) }
        throw error
    }
}

struct Message:Identifiable {
    let id=UUID()
    let role:String
    var content:String
}
@MainActor final class Chat:ObservableObject {
    let diagnostics = Diagnostics()
    private var generationID: UUID?
    func logError(_ error: Error, context: String, operation: UUID? = nil) {
        let detail = error as NSError
        let message = (error as? Issue)?.text ?? context
        diagnostics.record(operation == nil ? "error" : "end", message + " (" + detail.domain + " " + String(detail.code) + ")", level:"error", model:entry.name, operation:operation)
    }
    @Published var entry=Entry.choices[0]
    @Published var turns:[Message]=[]
    @Published var prompt=""
    @Published var status=""
    @Published var busy=false
    @Published var fetching=false
    @Published var byteCount: Int64 = 0
    @Published var expectedBytes: Int64 = 0
    private var transferID = UUID()
    @Published var jit=false
    @Published var installed=false
    var task:Task<Void,Never>?
    init(){
        do {
            try files.createDirectory(at:models,withIntermediateDirectories:true)
            var url=models,values=URLResourceValues();values.isExcludedFromBackup=true;try url.setResourceValues(values)
        } catch {logError(error, context:"تهيئة مجلد التطبيق");status=error.localizedDescription}
        if let saved = UserDefaults.standard.data(forKey: "modelFolder") {
            do { try chooseFolder(resolve(saved)) }
            catch { logError(error,context:"استعادة مجلد التنزيل"); folderAvailable = false; status = "تعذر فتح مجلد التنزيلات. أعد اختياره من الملفات." }
        }
        for (id, data) in links.sorted(by: { $0.key < $1.key }) {
            do { try register(resolve(data), id: id, persist: false) }
            catch { logError(error,context:"استعادة نموذج مضاف"); status = "تعذر فتح أحد النماذج المضافة. أعد اختياره من الملفات." }
        }
        refresh()
    }
    @Published private(set) var folder = models
    @Published private(set) var manual: [Entry] = []
    @Published private(set) var folderAvailable = true
    private var folderScope: URL?
    private var links: [String: Data] = UserDefaults.standard.dictionary(forKey: "modelLinks") as? [String: Data] ?? [:]
    private var externalURLs: [String: URL] = [:]
    @Published private(set) var catalogEntries = (UserDefaults.standard.data(forKey:"catalogEntries").flatMap { try? JSONDecoder().decode([Entry].self,from:$0) }) ?? []
    var choices: [Entry] { Entry.choices + catalogEntries + manual }
    func pickCatalog(_ item: Entry) {
        guard !busy else { return }
        if !choices.contains(where: { $0.id == item.id }) {
            catalogEntries.append(item)
            UserDefaults.standard.set(try? JSONEncoder().encode(catalogEntries),forKey:"catalogEntries")
        }
        select(item)
    }
    var isManual: Bool { entry.repo.hasPrefix("file:") }
    var path: URL { externalURLs[entry.id] ?? folder.appendingPathComponent(entry.localName) }

    private func bookmark(_ url: URL) throws -> Data {
        try url.bookmarkData(options: .minimalBookmark, includingResourceValuesForKeys: nil, relativeTo: nil)
    }

    private func resolve(_ data: Data) throws -> URL {
        var stale = false
        return try URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &stale)
    }

    func chooseFolder(_ url: URL) throws {
        let scoped = url.startAccessingSecurityScopedResource()
        do {
            guard try url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory == true,
                  files.isWritableFile(atPath: url.path) else { throw Issue("اختر مجلدا يسمح بحفظ الملفات.") }
            UserDefaults.standard.set(try bookmark(url), forKey: "modelFolder")
        } catch {
            if scoped { url.stopAccessingSecurityScopedResource() }
            throw error
        }
        folderScope?.stopAccessingSecurityScopedResource()
        folderScope = scoped ? url : nil
        diagnostics.record("folder","اختيار مجلد التنزيل")
        folder = url
        folderAvailable = true
        turns = []
        refresh()
    }

    private func register(_ url: URL, id: String, persist: Bool) throws {
        let scoped = url.startAccessingSecurityScopedResource()
        do {
            guard url.pathExtension.lowercased() == "gguf" else { throw Issue("اختر ملف GGUF.") }
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            guard try handle.read(upToCount: 4) == Data("GGUF".utf8) else { throw Issue("ترويسة الملف غير صالحة لنموذج GGUF.") }
            let length = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
            guard length > 4 else { throw Issue("ملف النموذج غير مكتمل.") }
            if persist {
                links[id] = try bookmark(url)
                UserDefaults.standard.set(links, forKey: "modelLinks")
            }
            manual.append(Entry(name: url.deletingPathExtension().lastPathComponent, repo: id,
                                file: url.lastPathComponent, estimate: Int64(length)))
            externalURLs[id] = url
            if !scoped { return }
            activeLinks.insert(id)
        } catch {
            if scoped { url.stopAccessingSecurityScopedResource() }
            throw error
        }
    }

    private var activeLinks: Set<String> = []
    func importModel(_ url: URL) throws {
        if let match = manual.first(where: { externalURLs[$0.id]?.standardizedFileURL == url.standardizedFileURL }) {
            select(match)
            return
        }
        let id = "file:" + UUID().uuidString
        try register(url, id: id, persist: true)
        diagnostics.record("import","إضافة نموذج محلي",model:url.lastPathComponent)
        if let item = manual.first(where: { $0.id == id }) { select(item) }
    }

    func refresh(){installed=(isManual || folderAvailable) && files.fileExists(atPath:path.path)}
    func select(_ value:Entry){guard !busy else{return};entry=value;diagnostics.record("select","اختيار النموذج",model:value.name);turns=[];status="";refresh()}
    func remove(){
        guard !busy else{return}
        if isManual {
            let id = entry.id
            if activeLinks.remove(id) != nil { externalURLs[id]?.stopAccessingSecurityScopedResource() }
            externalURLs.removeValue(forKey: id)
            manual.removeAll { $0.id == id }
            links.removeValue(forKey: id)
            UserDefaults.standard.set(links, forKey: "modelLinks")
            select(Entry.choices[0])
        } else {
            do {try files.removeItem(at:path);try? files.removeItem(at:path.appendingPathExtension("jinja"));turns=[];refresh()}catch{logError(error,context:"حذف النموذج");status=error.localizedDescription}
        }
    }
    func fetch(){
        guard !busy, folderAvailable, !isManual else{return};busy=true;fetching=true;status="قراءة معلومات الملف…"
        let current=entry
        let id = UUID(); transferID = id; byteCount = 0; expectedBytes = 0
        diagnostics.record("download","بدء تنزيل النموذج",model:current.name,operation:id)
        task=Task {
            defer{busy=false;fetching=false;refresh();task=nil}
            do {try await download(current,to:folder,progress:{ received,total in
                Task { @MainActor in
                    guard self.fetching, self.transferID == id else { return }
                    self.expectedBytes = total
                    self.byteCount = max(self.byteCount,min(total,received))
                }
            }) { self.status = $0; self.diagnostics.record("stage",$0,model:current.name,operation:id) };status="جاهز للعمل دون اتصال"; diagnostics.record("end",status,model:current.name,operation:id)}
            catch {
                status = Task.isCancelled ? "ألغي التنزيل" : error.localizedDescription
                if Task.isCancelled { diagnostics.record("end",status,model:current.name,operation:id) }
                else { logError(error,context:"فشل تنزيل النموذج بعد " + sizeText(byteCount),operation:id) }
            }
        }
    }
    func stop(){ diagnostics.record("stop","طلب إيقاف العملية",model:entry.name,operation:fetching ? transferID : generationID); if fetching {task?.cancel()}else{PLStop()} }
    func send(){
        let q=prompt.trimmingCharacters(in:.whitespacesAndNewlines)
        guard installed,!busy,!q.isEmpty else{return}
        let id = UUID()
        diagnostics.record("generate","طلب توليد محلي",model:entry.name,operation:id,prompt:q)
        guard q.utf8.count<100_000 else {status="الرسالة طويلة جدا";diagnostics.record("end",status,level:"warning",model:entry.name,operation:id);return}
        guard Int64(PLAvailable()) > entry.estimate+350_000_000 else {
            status="هامش RAM المتاح منخفض لهذا النموذج. أغلق التطبيقات الأخرى أو اختر نموذجا أصغر.";diagnostics.record("end",status,level:"warning",model:entry.name,operation:id);return
        }
        generationID = id
        PLResetStop();busy=true;status="تحميل النموذج إلى الذاكرة…";prompt=""
        turns.append(Message(role:"user",content:q))
        let history=turns.map{["role":$0.role,"content":$0.content]}
        let answer=Message(role:"assistant",content:"")
        turns.append(answer)
        let source=path.path,useJit=jit
        Task {
            let result=await Task.detached(priority:.userInitiated){() -> (String,String?) in
                var error:NSString?
                let reply=PLGenerate(source,history,useJit,{ draft in
                    Task { @MainActor in
                        if self.busy,self.turns.last?.id==answer.id {self.turns[self.turns.count-1].content=draft;self.status="جار التوليد على الجهاز…"}
                    }
                },&error)
                return (reply,error as String?)
            }.value
            if result.0.isEmpty {
                turns.removeLast(2);prompt=q
            } else {turns[turns.count-1].content=result.0}
            status=result.1 ?? (result.0.isEmpty ? "توقف التوليد دون نص" : "اكتمل الرد المحلي")
            diagnostics.record("end",status,level:result.1 == nil ? "info" : "error",model:entry.name,operation:id)
            generationID = nil
            busy=false
        }
    }
}
@main struct PadLocalApp:App {
    @StateObject var chat=Chat()
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase
    @State private var attached = PLDebugAttached()
    @State private var tunnelMessage = ""
    @State private var confirmDelete=false
    @State private var catalogOpen = false
    @State private var pickingFolder = false
    @State private var picking = false
    var body:some Scene {WindowGroup {NavigationStack {
        List {
            Section("النموذج المحلي"){
                Button("تصفح قوائم التنزيل"){catalogOpen=true}.disabled(chat.busy)
                Picker("النموذج",selection:Binding(get:{chat.entry.id},set:{id in if let e=chat.choices.first(where:{$0.id==id}){chat.select(e)}})){
                    ForEach(chat.choices){Text($0.name).tag($0.id)}
                }.disabled(chat.busy)
                Text((chat.isManual ? "حجم الملف " : "حجم التنزيل التقريبي ")+sizeText(chat.entry.estimate))
                Text("مجلد التنزيلات: " + (chat.folderAvailable ? chat.folder.lastPathComponent : "غير متاح"))
                Button("اختيار مجلد التنزيلات"){ pickingFolder=true; picking=true }.disabled(chat.busy)
                Button("إضافة ملف GGUF موجود"){ pickingFolder=false; picking=true }.disabled(chat.busy)
                if chat.isManual {Button("إزالة من القائمة"){chat.remove()}.disabled(chat.busy)}
                else if chat.installed {Button("حذف الأوزان من الجهاز",role:.destructive){confirmDelete=true}.disabled(chat.busy)}
                else {Button("تنزيل النموذج"){chat.fetch()}.disabled(chat.busy || !chat.folderAvailable)}
            }
            if chat.fetching {
                Section("تقدم التنزيل") {
                    if chat.expectedBytes > 0 {
                        let fraction = Double(chat.byteCount) / Double(chat.expectedBytes)
                        ProgressView(value:fraction)
                        Text(sizeText(chat.byteCount) + " من " + sizeText(chat.expectedBytes) + " · " + fraction.formatted(.percent.precision(.fractionLength(1))))
                            .monospacedDigit()
                    } else { ProgressView() }
                    Text(chat.status)
                }
            }
            NavigationLink("سجل التشخيص") { DiagnosticsView(log:chat.diagnostics) }
            Section("موارد الجهاز"){
                TimelineView(.periodic(from:.now,by:2)){_ in
                    VStack(alignment:.leading){
                        Text("RAM المتاحة للتطبيق: "+sizeText(Int64(PLAvailable())))
                        Text("التخزين المتاح: "+sizeText(disk(chat.folder)))
                    }.monospacedDigit()
                }
                Button("فتح LocalDevVPN للاتصال") {
                    guard let url = URL(string: "localdevvpn://enable") else { return }
                    openURL(url) { opened in
                        tunnelMessage = opened ? "ارجع إلى PadLocal بعد الاتصال." : "تعذر فتح LocalDevVPN. تأكد من تثبيته."
                    }
                }.disabled(chat.busy)
                if !tunnelMessage.isEmpty { Text(tunnelMessage).font(.footnote) }
                Button("فحص اتصال المصحح") { attached = PLDebugAttached() }
                Text(attached ? "المصحح متصل" : "بانتظار اتصال المصحح").font(.footnote)
                Toggle("CPU JIT التجريبي لعقوبة التكرار",isOn:$chat.jit).disabled(chat.busy || (!attached && !chat.jit))
                Text("الاستدلال يستخدم Metal. يتطلب خيار JIT مصححا متصلا وإذن تنفيذ من النظام.").font(.footnote)
            }
            Section("المحادثة المحلية"){
                ForEach(chat.turns){m in VStack(alignment:.leading){Text(m.role=="user" ? "أنت" : chat.entry.name).font(.caption);Text(m.content).textSelection(.enabled)}}
                TextField("اكتب رسالتك",text:$chat.prompt,axis:.vertical).lineLimit(1...5)
                if chat.busy {Button("إيقاف"){chat.stop()}}
                else {Button("إرسال"){chat.send()}.disabled(!chat.installed || chat.prompt.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty)}
                if !chat.turns.isEmpty {Button("محادثة جديدة"){chat.turns=[];chat.status=""}.disabled(chat.busy)}
                if !chat.status.isEmpty {Text(chat.status).font(.footnote)}
            }
        }.navigationTitle("PadLocal")
        .sheet(isPresented:$catalogOpen) {
            ModelCatalog { item in chat.pickCatalog(item); catalogOpen = false }
        }
        .fileImporter(isPresented: $picking, allowedContentTypes: pickingFolder ? [.folder] : [UTType(filenameExtension:"gguf") ?? .data]) { result in
            do {
                let url = try result.get()
                if pickingFolder { try chat.chooseFolder(url) } else { try chat.importModel(url) }
            } catch where (error as NSError).code == NSUserCancelledError { }
              catch { chat.logError(error,context:"اختيار ملف أو مجلد"); chat.status = error.localizedDescription }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { attached = PLDebugAttached() }
        }
        .confirmationDialog("حذف أوزان هذا النموذج؟",isPresented:$confirmDelete){Button("حذف",role:.destructive){chat.remove()}}
    }.environment(\.layoutDirection,.rightToLeft)}}
}
