import SwiftUI
import UniformTypeIdentifiers

struct DiagnosticEvent: Codable, Identifiable {
    let id: UUID
    let time: Date
    let session: UUID
    let operation: UUID?
    let level: String
    let action: String
    let message: String
    let model: String?
    let availableRAM: UInt64
    let appVersion: String
    let system: String
    let prompt: String?
}

@MainActor
final class Diagnostics: ObservableObject {
    @Published private(set) var events: [DiagnosticEvent] = []
    @Published private(set) var storageError = ""
    @Published var savePrompts = UserDefaults.standard.bool(forKey: "diagnosticPrompts") {
        didSet { UserDefaults.standard.set(savePrompts, forKey: "diagnosticPrompts") }
    }
    private let session = UUID()
    private let file: URL
    private let version: String
    private let system = ProcessInfo.processInfo.operatingSystemVersionString

    init() {
        let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Diagnostics", isDirectory: true)
        file = directory.appendingPathComponent("events.json")
        version = (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "?"
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            var folder = directory
            var attributes = URLResourceValues()
            attributes.isExcludedFromBackup = true
            try folder.setResourceValues(attributes)
            if FileManager.default.fileExists(atPath: file.path) {
                let decoder = JSONDecoder()
                decoder.dateDecodingStrategy = .iso8601
                events = try decoder.decode([DiagnosticEvent].self, from: Data(contentsOf: file))
            }
        } catch { storageError = error.localizedDescription; return }
        let unfinished = Dictionary(grouping: events.compactMap { event in event.operation.map { ($0, event) } }, by: { $0.0 })
            .values.compactMap { $0.last?.1 }.filter { !["end","interrupted"].contains($0.action) }
        for event in unfinished {
            record("interrupted", "انتهت الجلسة السابقة قبل تسجيل نتيجة العملية.", level: "warning", model: event.model, operation: event.operation)
        }
        record("launch", "فتح التطبيق")
    }

    func record(_ action: String, _ message: String, level: String = "info", model: String? = nil,
                operation: UUID? = nil, prompt: String? = nil) {
        events.append(DiagnosticEvent(id: UUID(), time: Date(), session: session, operation: operation,
            level: level, action: action, message: String(message.prefix(1024)), model: model,
            availableRAM: PLAvailable(), appVersion: version, system: system,
            prompt: savePrompts ? prompt.map { String($0.prefix(2048)) } : nil))
        if events.count > 400 { events.removeFirst(events.count - 400) }
        persist()
    }

    func clear() { events.removeAll(); persist() }

    static func encode(_ events: [DiagnosticEvent]) throws -> Data {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return try encoder.encode(events)
    }

    private func persist() {
        do {
            try Self.encode(events).write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
            storageError = ""
        } catch { storageError = error.localizedDescription }
    }
}

private struct LogDocument: FileDocument {
    static var readableContentTypes: [UTType] { [.json] }
    let data: Data
    init(_ data: Data) { self.data = data }
    init(configuration: ReadConfiguration) throws { data = configuration.file.regularFileContents ?? Data() }
    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper { FileWrapper(regularFileWithContents: data) }
}

struct DiagnosticsView: View {
    @ObservedObject var log: Diagnostics
    @State private var problemsOnly = false
    @State private var confirmClear = false
    @State private var exporting = false
    @State private var document: LogDocument?
    @State private var exportError = ""
    var body: some View {
        List {
            Toggle("حفظ نص الأوامر في السجل", isOn: $log.savePrompts)
            Text("يحتفظ الجهاز بآخر 400 حدث. عند تفعيل حفظ الأوامر يُحفظ أول 2048 حرفا من كل طلب جديد وتظهر في الملف المصدّر.").font(.footnote)
            Toggle("عرض الأخطاء والتنبيهات فقط", isOn: $problemsOnly)
            Button("تصدير السجل الكامل بصيغة JSON") {
                do { document = try LogDocument(Diagnostics.encode(log.events)); exporting = true; exportError = "" }
                catch { exportError = error.localizedDescription }
            }
            Button("مسح السجل", role: .destructive) { confirmClear = true }
            if !log.storageError.isEmpty { Text(log.storageError) }
            if !exportError.isEmpty { Text(exportError) }
            ForEach(log.events.reversed().filter { !problemsOnly || $0.level != "info" }) { event in
                VStack(alignment: .leading, spacing: 3) {
                    Text(event.message)
                    Text(event.time.formatted(date:.numeric,time:.standard) + " · " + event.level).font(.caption)
                    if let model = event.model { Text(model).font(.caption) }
                    if let prompt = event.prompt { Text(prompt).textSelection(.enabled) }
                }
            }
        }.navigationTitle("سجل التشخيص")
        .fileExporter(isPresented: $exporting, document: document, contentType: .json, defaultFilename: "PadLocal-diagnostics") { result in
            if case .failure(let error) = result { exportError = error.localizedDescription }
        }
        .confirmationDialog("مسح الأحداث والأوامر المحفوظة؟", isPresented: $confirmClear) {
            Button("مسح السجل", role: .destructive) { log.clear() }
        }
    }
}
