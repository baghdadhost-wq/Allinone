import SwiftUI

private enum Specialty: String, CaseIterable {
    case coding = "البرمجة", vision = "فهم الصور", art = "توليد الصور", math = "الرياضيات"
    case reasoning = "الاستدلال", translation = "الترجمة", speech = "الصوت", chat = "المحادثة والنصوص"

    var signals: [String] {
        switch self {
        case .coding: return ["coder","code","coding","programming","starcoder","codellama","codestral","codegen"]
        case .vision: return ["vision","vl","llava","ocr","image-text-to-text","image-to-text","visual-question-answering"]
        case .art: return ["text-to-image","image-to-image","stable-diffusion","flux"]
        case .math: return ["math","mathematics","mathstral"]
        case .reasoning: return ["reasoning","reasoner","thinking","deepseek-r1"]
        case .translation: return ["translation","translate","translator"]
        case .speech: return ["whisper","automatic-speech-recognition","text-to-speech","text-to-audio","audio-text-to-text"]
        case .chat: return ["chat","instruct","conversational","text-generation"]
        }
    }
    var keywords: [String] {
        switch self {
        case .coding: return ["برمجة","البرمجة","للبرمجة","كود","اكواد","coding","programming"]
        case .vision: return ["صور","الصور","للصور","رؤية","vision","images","ocr"]
        case .art: return ["صور","الصور","للصور","رسم","images","diffusion"]
        case .math: return ["رياضيات","الرياضيات","math"]
        case .reasoning: return ["استدلال","الاستدلال","تفكير","reasoning"]
        case .translation: return ["ترجمة","الترجمة","translation"]
        case .speech: return ["صوت","الصوت","صوتيات","audio"]
        case .chat: return ["محادثة","دردشة","نصوص","chat"]
        }
    }
}

private struct CatalogQuery {
    var topics = Set<Specialty>()
    var name: String
    init(_ input: String) {
        var words: [String] = []
        var phrase = input
        for (text, topic) in [("توليد الصور", Specialty.art), ("فهم الصور", .vision), ("تحليل الصور", .vision)] where phrase.contains(text) {
            topics.insert(topic)
            phrase = phrase.replacingOccurrences(of:text, with:" ")
        }
        for word in phrase.split(whereSeparator: { $0.isWhitespace }) {
            let matches = Specialty.allCases.filter { $0.keywords.contains(word.lowercased()) }
            if matches.isEmpty { words.append(String(word)) } else { topics.formUnion(matches) }
        }
        name = words.joined(separator:" ")
    }
}

private struct HubModel: Decodable, Identifiable {
    let id: String
    let tags: [String]?
    let pipeline_tag: String?
    var specialties: [Specialty] {
        let fields = [String(id.split(separator:"/").last ?? "")] + (tags ?? []) + [pipeline_tag ?? ""]
        let tokens = fields.map { "-" + $0.lowercased().components(separatedBy:CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty }.joined(separator:"-") + "-" }
        return Specialty.allCases.filter { topic in
            topic.signals.contains { signal in tokens.contains { $0.contains("-" + signal + "-") } }
        }
    }
    var label: String {
        specialties.isEmpty ? "اختصاص غير محدد" : specialties.map(\.rawValue).joined(separator:" · ")
    }
    var marked: Bool {
        let labels = ([id] + (tags ?? [])).joined(separator: " ").lowercased()
        return ["uncensor", "abliterat", "heretic", "unfilter", "refusal-free", "no-refusal"].contains { labels.contains($0) }
    }
}

private func hubPage(search: String, next: URL?) async throws -> ([HubModel], URL?) {
    var api = URLComponents(string: "https://huggingface.co/api/models")!
    api.queryItems = [.init(name:"filter",value:"gguf"), .init(name:"sort",value:"downloads"),
                      .init(name:"direction",value:"-1"), .init(name:"limit",value:"100"), .init(name:"full",value:"true")]
    if !search.isEmpty { api.queryItems?.append(.init(name:"search",value:search)) }
    let (data, response) = try await URLSession.shared.data(from: next ?? api.url!)
    guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { throw Issue("تعذر جلب قائمة النماذج. حاول مرة أخرى.") }
    var following: URL?
    for part in (http.value(forHTTPHeaderField:"Link") ?? "").components(separatedBy:",") where part.contains("rel=\"next\"") {
        if let left = part.firstIndex(of:"<"), let right = part.firstIndex(of:">"), left < right,
           let url = URL(string:String(part[part.index(after:left)..<right])), url.scheme == "https", url.host == "huggingface.co" {
            following = url
        }
    }
    return (try JSONDecoder().decode([HubModel].self, from:data), following)
}

struct ModelCatalog: View {
    var choose: (Entry) -> Void
    @State private var marked = false
    @State private var topic: Specialty?
    @State private var query = ""
    @State private var rows: [HubModel] = []
    @State private var next: URL?
    @State private var page = 0
    @State private var busy = false
    @State private var issue = ""
    private var key: String { "\(marked)|\(topic?.rawValue ?? "")|\(query)|\(page)" }

    var body: some View {
        NavigationStack {
            List {
                Picker("القائمة", selection:$marked) {
                    Text("العادية").tag(false)
                    Text("Uncensored").tag(true)
                }.pickerStyle(.segmented)
                Picker("الاختصاص", selection:$topic) {
                    Text("كل الاختصاصات").tag(nil as Specialty?)
                    ForEach(Specialty.allCases, id:\.self) { Text($0.rawValue).tag(Optional($0)) }
                }
                Text("التصنيف حسب اسم المستودع ووسوم الناشر. قد توجد نسخ معدلة بلا وسم.").font(.footnote)
                if marked && query.isEmpty && topic == nil {
                    ForEach(Entry.choices) { item in
                        Button { choose(item) } label: { VStack(alignment:.leading) { Text(item.name); Text("محادثة نصية").font(.caption) } }
                    }
                }
                ForEach(rows) { model in
                    NavigationLink {
                        CatalogFiles(model:model, choose:choose)
                    } label: {
                        VStack(alignment:.leading) { Text(model.id); Text(model.label).font(.caption).foregroundStyle(.secondary) }
                    }
                }
                if busy { ProgressView() }
                if !issue.isEmpty { Text(issue); Button("إعادة المحاولة") { page += 1 } }
                else if !busy && next != nil { Button("عرض المزيد") { page += 1 } }
                else if !busy && rows.isEmpty { Text("لا توجد نتائج مطابقة في الفهرس.") }
            }
            .navigationTitle("تنزيل النماذج")
            .searchable(text:$query, prompt:"الاسم أو الاختصاص، مثلا Qwen برمجة")
            .task(id:key) {
                let reset = loadedQuery != query || loadedGroup != marked || loadedTopic != topic
                if reset { rows = []; next = nil; loadedQuery = query; loadedGroup = marked; loadedTopic = topic }
                busy = true; issue = ""
                do {
                    try await Task.sleep(nanoseconds:300_000_000)
                    let search = CatalogQuery(query)
                    var cursor = next
                    var found: [HubModel] = []
                    for _ in 0..<3 {
                        let batch = try await hubPage(search:search.name, next:cursor)
                        try Task.checkCancellation()
                        found += batch.0.filter { model in
                            guard model.marked == marked else { return false }
                            let categories = Set(model.specialties)
                            return (topic.map { categories.contains($0) } ?? true)
                                && (search.topics.isEmpty || !categories.isDisjoint(with:search.topics))
                        }
                        cursor = batch.1
                        if !found.isEmpty || cursor == nil { break }
                    }
                    var ids = Set(rows.map(\.id))
                    rows += found.filter { ids.insert($0.id).inserted }
                    next = cursor
                } catch { if Task.isCancelled { return }; issue = error.localizedDescription }
                busy = false
            }
        }
    }
    @State private var loadedQuery: String?
    @State private var loadedGroup: Bool?
    @State private var loadedTopic: Specialty?
}

private struct CatalogFiles: View {
    let model: HubModel
    var repo: String { model.id }
    var choose: (Entry) -> Void
    @State private var options: [Entry] = []
    @State private var status = "جار جلب ملفات النموذج…"
    @State private var attempt = 0
    var body: some View {
        List {
            Text(model.label)
            if !Set(model.specialties).isDisjoint(with:[.vision,.art,.speech]) {
                Text("واجهة PadLocal الحالية تدعم المحادثة النصية فقط. إدخال الصور أو الصوت وتوليدهما يحتاج إلى محرك مناسب.").font(.footnote)
            }
            Text("اختر حجم الضغط المناسب لجهازك. التشغيل يعتمد على دعم المحرك لبنية النموذج وعلى الذاكرة المتاحة.").font(.footnote)
            ForEach(options) { entry in
                Button { choose(entry) } label: {
                    VStack(alignment:.leading) { Text(entry.file); Text(sizeText(entry.estimate)).font(.caption) }
                }
            }
            if !status.isEmpty { Text(status) }
            if options.isEmpty { Button("تحديث"){attempt += 1} }
        }.navigationTitle(repo.components(separatedBy:"/").last ?? repo)
        .task(id:attempt) {
            do {
                let url = URL(string:"https://huggingface.co/api/models")!.appendingPathComponent(repo)
                var components = URLComponents(url:url,resolvingAgainstBaseURL:false)!
                components.queryItems = [.init(name:"blobs",value:"true")]
                let (data,response) = try await URLSession.shared.data(from:components.url!)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw Issue("تعذر الوصول إلى ملفات المستودع.") }
                let card = try JSONDecoder().decode(Card.self,from:data)
                options = card.siblings.compactMap { item in
                    let name = item.rfilename.lowercased()
                    guard name.hasSuffix(".gguf"), !name.contains("mmproj"), !name.contains("-of-"),
                          !name.contains("imatrix"), !item.rfilename.split(separator:"/").contains(".."),
                          let blob = item.lfs, blob.size > 0 else { return nil }
                    return Entry(name:repo + " · " + item.rfilename,repo:repo,file:item.rfilename,estimate:blob.size)
                }.sorted { $0.estimate < $1.estimate }
                status = options.isEmpty ? "لا يوجد ملف GGUF منفرد مناسب للتنزيل." : ""
            } catch { status = error.localizedDescription }
        }
    }
}
