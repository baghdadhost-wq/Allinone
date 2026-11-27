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
func download(_ entry:Entry,to folder:URL,status:@escaping @Sendable (String)->Void) async throws {
    let api=URL(string:"https://huggingface.co/api/models/"+entry.repo+"?blobs=true")!
    let (metadata,response)=try await URLSession.shared.data(from:api)
    guard (response as? HTTPURLResponse)?.statusCode == 200 else {throw Issue("تعذرت قراءة معلومات النموذج.")}
    let card=try JSONDecoder().decode(Card.self,from:metadata)
    guard let blob=card.siblings.first(where:{$0.rfilename==entry.file})?.lfs, blob.size>0,
          blob.sha256.count==64,card.sha.count==40 else {throw Issue("تعذر التحقق من بصمة الملف.")}
    guard disk(folder)>blob.size+300_000_000 else {throw Issue("مساحة التخزين المتاحة غير كافية.")}
    status("تنزيل "+sizeText(blob.size)+"…")
    let url=URL(string:"https://huggingface.co")!.appendingPathComponent(entry.repo).appendingPathComponent("resolve").appendingPathComponent(card.sha).appendingPathComponent(entry.file)
    let (temp,reply)=try await URLSession.shared.download(from:url)
    defer {try? files.removeItem(at:temp)}
    guard (reply as? HTTPURLResponse)?.statusCode==200 else {throw Issue("فشل تنزيل الأوزان.")}
    status("التحقق من سلامة الأوزان…")
    let handle=try FileHandle(forReadingFrom:temp)
    defer {try? handle.close()}
    var hash=SHA256(),length:Int64=0
    while let data=try handle.read(upToCount:1<<20), !data.isEmpty {
        try Task.checkCancellation()
        hash.update(data:data);length+=Int64(data.count)
    }
    guard length==blob.size,hash.finalize().map({String(format:"%02x",$0)}).joined()==blob.sha256.lowercased() else {throw Issue("بصمة الملف لا تطابق النسخة المنشورة.")}
    try Task.checkCancellation()
    try files.moveItem(at:temp,to:folder.appendingPathComponent(entry.localName))
}

struct Message:Identifiable {
    let id=UUID()
    let role:String
    var content:String
}
@MainActor final class Chat:ObservableObject {
    @Published var entry=Entry.choices[0]
    @Published var turns:[Message]=[]
    @Published var prompt=""
    @Published var status=""
    @Published var busy=false
    @Published var fetching=false
    @Published var jit=false
    @Published var installed=false
    var task:Task<Void,Never>?
    init(){
        do {
            try files.createDirectory(at:models,withIntermediateDirectories:true)
            var url=models,values=URLResourceValues();values.isExcludedFromBackup=true;try url.setResourceValues(values)
        } catch {status=error.localizedDescription}
        if let saved = UserDefaults.standard.data(forKey: "modelFolder") {
            do { try chooseFolder(resolve(saved)) }
            catch { folderAvailable = false; status = "تعذر فتح مجلد التنزيلات. أعد اختياره من الملفات." }
        }
        for (id, data) in links.sorted(by: { $0.key < $1.key }) {
            do { try register(resolve(data), id: id, persist: false) }
            catch { status = "تعذر فتح أحد النماذج المضافة. أعد اختياره من الملفات." }
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
        if let item = manual.first(where: { $0.id == id }) { select(item) }
    }

    func refresh(){installed=(isManual || folderAvailable) && files.fileExists(atPath:path.path)}
    func select(_ value:Entry){guard !busy else{return};entry=value;turns=[];status="";refresh()}
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
            do {try files.removeItem(at:path);turns=[];refresh()}catch{status=error.localizedDescription}
        }
    }
    func fetch(){
        guard !busy, folderAvailable, !isManual else{return};busy=true;fetching=true;status="قراءة معلومات الملف…"
        let current=entry
        task=Task {
            defer{busy=false;fetching=false;refresh();task=nil}
            do {try await download(current,to:folder){msg in Task{@MainActor in self.status=msg}};status="جاهز للعمل دون اتصال"}
            catch {status=Task.isCancelled ? "ألغي التنزيل" : error.localizedDescription}
        }
    }
    func stop(){ if fetching {task?.cancel()}else{PLStop()} }
    func send(){
        let q=prompt.trimmingCharacters(in:.whitespacesAndNewlines)
        guard installed,!busy,!q.isEmpty else{return}
        guard q.utf8.count<100_000 else {status="الرسالة طويلة جدا";return}
        guard Int64(PLAvailable()) > entry.estimate+350_000_000 else {
            status="هامش RAM المتاح منخفض لهذا النموذج. أغلق التطبيقات الأخرى أو اختر نموذجا أصغر.";return
        }
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
              catch { chat.status = error.localizedDescription }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { attached = PLDebugAttached() }
        }
        .confirmationDialog("حذف أوزان هذا النموذج؟",isPresented:$confirmDelete){Button("حذف",role:.destructive){chat.remove()}}
    }.environment(\.layoutDirection,.rightToLeft)}}
}
