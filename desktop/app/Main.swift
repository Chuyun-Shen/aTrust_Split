import AppKit
import Foundation
import CoreFoundation
import Network
import UserNotifications
import Darwin

/// Decides when to help with a login: once per logout episode, only after the
/// logout has persisted for `hold` seconds and the user can act on it.
struct LoginGate {
    enum Event: Equatable { case prompt, recovered }
    var since: Date? = nil
    var prompted = false

    mutating func update(need: Bool, connected: Bool, ready: Bool, now: Date = Date(),
                         hold: TimeInterval = 15) -> Event? {
        if connected {
            defer { since = nil; prompted = false }
            return prompted ? .recovered : nil
        }
        guard need else { since = nil; prompted = false; return nil }
        let start = since ?? now
        since = start
        guard ready, !prompted, now.timeIntervalSince(start) >= hold else { return nil }
        prompted = true
        return .prompt
    }

    /// After sleep the user may have missed the earlier prompt.
    mutating func rearm() { prompted = false }
}

struct LocalProfile {
    let id: String
    let name: String
    let shortName: String
    let loginURL: URL?
}

/// Installed settings are authoritative; missing/invalid settings only provide
/// inert generic menu labels, never a portal or an enabled connection.
struct LocalProfiles {
    static let path = "/Library/Application Support/ATrustSplit/settings.json"
    static let ids = ["a", "b"]
    private var values: [String: LocalProfile] = [
        "a": LocalProfile(id: "a", name: "机构 A", shortName: "A", loginURL: nil),
        "b": LocalProfile(id: "b", name: "机构 B", shortName: "B", loginURL: nil)]
    var configured = false
    init(data: Data? = nil) {
        guard let data,
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let version = object["version"] as? NSNumber, CFGetTypeID(version) != CFBooleanGetTypeID(),
              version.doubleValue == 1,
              let rows = object["profiles"] as? [[String: Any]], rows.count == 2 else { return }
        var parsed: [String: LocalProfile] = [:]
        var actual = true
        for row in rows {
            guard let id = row["id"] as? String, Self.ids.contains(id), parsed[id] == nil,
                  let name = row["name"] as? String, !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  let short = row["short_name"] as? String, !short.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  let login = row["login_url"] as? String, let url = URL(string: login),
                  url.scheme?.lowercased() == "https", let host = url.host?.lowercased(),
                  url.user == nil, url.password == nil,
                  let gateways = row["gateways"] as? [String], gateways.contains(host),
                  let domains = row["domains"] as? [String], !domains.isEmpty else { return }
            let hosts = domains + gateways
            if hosts.contains(where: { $0 == "example.org" || $0.hasSuffix(".example.org") ||
                $0 == "example.com" || $0.hasSuffix(".example.com") ||
                $0 == "example.net" || $0.hasSuffix(".example.net") }) { actual = false }
            parsed[id] = LocalProfile(id: id, name: name, shortName: short, loginURL: url)
        }
        guard Set(parsed.keys) == Set(Self.ids) else { return }
        values = parsed
        configured = actual
    }
    static func installed() -> Self {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: path),
              (attributes[.ownerAccountID] as? NSNumber)?.intValue == 0,
              let mode = attributes[.posixPermissions] as? NSNumber, mode.intValue & 0o022 == 0,
              attributes[.type] as? FileAttributeType == .typeRegular else { return Self() }
        return Self(data: try? Data(contentsOf: URL(fileURLWithPath: path)))
    }
    subscript(_ id: String) -> LocalProfile { values[id] ?? values["a"]! }
}

struct ContainerStatus {
    var school = "a"
    var phase = "not-configured"
    var message = "容器尚未安装"
    var enabled = false
    var fresh = false
    var memoryMB = 0
    init(data: Data? = nil) {
        guard let data, let object = try? JSONSerialization.jsonObject(with: data),
              let json = object as? [String: Any] else { return }
        school = json["school"] as? String ?? "a"
        phase = json["phase"] as? String ?? "unknown"
        message = json["message"] as? String ?? "等待状态"
        enabled = json["enabled"] as? Bool ?? false
        memoryMB = json["memory_mb"] as? Int ?? 0
        let age = Date().timeIntervalSince1970 - (json["updated_at"] as? Double ?? 0)
        fresh = age >= 0 && age < 45
        if !fresh && phase != "starting" && phase != "stopping" {
            message = "容器后台暂未响应"
        }
    }
    var connected: Bool { phase == "connected" && fresh }
    var needsLogin: Bool { phase == "login-required" && fresh && enabled }

    /// One short phrase for the menu, plus the colour of its status dot.
    var summary: (String, NSColor) {
        if !fresh && phase != "not-configured" { return ("后台未响应", .systemRed) }
        switch phase {
        case "connected": return ("已连接", .systemGreen)
        case "login-required": return ("需要登录", .systemOrange)
        case "reconnecting": return ("线路重连中", .systemYellow)
        case "offline": return ("网络不可用", .systemOrange)
        case "unreachable": return ("业务不可达", .systemRed)
        case "paused", "stopping", "stopped": return ("已停止", .tertiaryLabelColor)
        case "not-configured": return ("未安装", .tertiaryLabelColor)
        case "starting": return ("启动中…", .systemYellow)
        default: return ("检查中…", .systemYellow)
        }
    }
}

struct ServiceStatus {
    var phase = "not-installed"
    var message = "后台服务尚未安装"
    var enabled = true
    var selection = "auto"
    var shortName: String? = nil
    var profileID: String? = nil
    var assignment: [String: Any] = [:]
    var routesRestored = false
    var gateway = ""
    var routeCount = 0
    var error = ""
    var containerRouteError = ""
    var fresh = false

    init(data: Data? = nil) {
        guard let data, let object = try? JSONSerialization.jsonObject(with: data),
              let json = object as? [String: Any] else { return }
        phase = json["phase"] as? String ?? "unknown"
        message = json["message"] as? String ?? "等待状态"
        enabled = json["enabled"] as? Bool ?? true
        selection = json["selection"] as? String ?? "auto"
        shortName = json["short_name"] as? String
        profileID = json["profile_id"] as? String
        assignment = json["assignment"] as? [String: Any] ?? [:]
        routesRestored = json["routes_restored"] as? Bool ?? false
        gateway = json["gateway"] as? String ?? ""
        routeCount = json["managed_routes"] as? Int ?? 0
        error = json["last_error"] as? String ?? ""
        containerRouteError = json["container_route_error"] as? String ?? ""
        let age = Date().timeIntervalSince1970 - (json["updated_at"] as? Double ?? 0)
        fresh = age >= 0 && age < 30
        if !fresh && phase != "stopped" {
            phase = "stale"
            message = "后台服务暂未响应"
        }
    }

    var needsLogin: Bool { phase == "logged-out" && fresh && enabled }

    var title: String {
        if phase == "active" { return "分流运行中 · \(shortName ?? "VPN")" }
        return message
    }

    var summary: (String, NSColor) {
        switch phase {
        case "active": return ("已连接", .systemGreen)
        case "logged-out": return ("需要登录", .systemOrange)
        case "degraded": return ("DNS 异常", .systemRed)
        case "paused": return ("分流已暂停", .tertiaryLabelColor)
        case "stale": return ("后台未响应", .systemRed)
        case "not-installed", "stopped": return ("未运行", .tertiaryLabelColor)
        default: return ("连接中…", .systemYellow)
        }
    }
}

struct ConnectionChoices {
    var enabled: Set<String> = []
    var nativePreference = "a"
    var revision: Int64 = 0
    var valid = false
    init(data: Data? = nil) {
        guard let data, let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let version = json["version"] as? NSNumber, CFGetTypeID(version) != CFBooleanGetTypeID(),
              version.doubleValue == 1,
              let values = json["enabled"] as? [String], let preference = json["native_preference"] as? String,
              Set(values).isSubset(of: Set(LocalProfiles.ids)), Set(values).count == values.count,
              LocalProfiles.ids.contains(preference) else { return }
        if let raw = json["revision"] {
            guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  let revision = Int64(number.stringValue), revision >= 0 else { return }
            self.revision = revision
        }
        enabled = Set(values)
        nativePreference = preference
        valid = true
    }
    var native: String? { enabled.count == 2 ? nativePreference : enabled.first }
    var container: String? { enabled.first(where: { $0 != native }) }
    func nativeLoginURL(profiles: LocalProfiles) -> URL? {
        native.flatMap { profiles[$0].loginURL }
    }
}

/// Quit the official window once the exact deselection has finished restoring
/// networking. A later manual launch is left alone until another deselection.
struct NativeQuitGate {
    private var handledRevision: Int64? = nil
    mutating func update(choices: ConnectionChoices, manager: [String: Any],
                         service: ServiceStatus, now: Date = Date()) -> Bool {
        guard choices.valid else { return false }
        guard choices.enabled.isEmpty else { handledRevision = nil; return false }
        let native = manager["native_status"] as? [String: Any] ?? [:]
        let age = now.timeIntervalSince1970 - (manager["updated_at"] as? Double ?? 0)
        guard handledRevision == nil, choices.revision > 0,
              manager["revision"] as? Int64 == choices.revision,
              manager["phase"] as? String == "ready", age >= 0, age < 45,
              manager["native"] is NSNull, manager["container"] is NSNull,
              native["phase"] as? String == "stopped",
              service.fresh, !service.enabled, service.phase == "paused",
              service.routeCount == 0, service.containerRouteError.isEmpty, service.routesRestored,
              service.assignment["revision"] as? Int64 == choices.revision,
              service.assignment["native"] is NSNull, service.assignment["container"] is NSNull else { return false }
        handledRevision = choices.revision
        return true
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, UNUserNotificationCenterDelegate {
    private var statusItem: NSStatusItem!
    private var timer: Timer?
    private var status = ServiceStatus()
    private var isolated = ContainerStatus()
    private var choices = ConnectionChoices()
    private var manager: [String: Any] = [:]
    private var profiles = LocalProfiles()
    private var smoke = false
    private var loginAssignment: [String?] = []
    private var working = false
    private let connectionDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/ATrustDual")
    private var menuOpen = false
    private let statusURL = URL(fileURLWithPath: "/var/run/atrust-split/status.json")
    private let aTrustApps = ["/Applications/aTrust.app", "/Library/sangfor/SDP/aTrust.app"]
        .map { URL(fileURLWithPath: $0) }
    private var nativeGate = LoginGate()
    private var containerGate = LoginGate()
    private var nativeQuitGate = NativeQuitGate()
    private var settledAfter = Date().addingTimeInterval(30)
    private var screenLocked = false
    private var networkUp = true
    private let pathMonitor = NWPathMonitor()
    private var autoLogin: Bool {
        get { !UserDefaults.standard.bool(forKey: "disableAutoLogin") }
        set { UserDefaults.standard.set(!newValue, forKey: "disableAutoLogin") }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        profiles = LocalProfiles.installed()
        let existing = NSRunningApplication.runningApplications(withBundleIdentifier: "io.github.chuyun-shen.atrust-split")
            .filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
        if !existing.isEmpty { NSApp.terminate(nil); return }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        observeEnvironment()
        // Login helpers run only for selected institutions, after reading choices.
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            guard let self, !self.menuOpen else { return }
            self.refresh()
        }
        NSApp.setActivationPolicy(.accessory)
    }

    private func observeEnvironment() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            // Give Wi-Fi and both aTrust clients time to reconnect on their own.
            self.settledAfter = Date().addingTimeInterval(30)
            self.nativeGate.rearm()
            self.containerGate.rearm()
        }
        let distributed = DistributedNotificationCenter.default()
        distributed.addObserver(forName: .init("com.apple.screenIsLocked"), object: nil, queue: .main) { [weak self] _ in
            self?.screenLocked = true
        }
        distributed.addObserver(forName: .init("com.apple.screenIsUnlocked"), object: nil, queue: .main) { [weak self] _ in
            self?.screenLocked = false
        }
        pathMonitor.pathUpdateHandler = { [weak self] path in
            DispatchQueue.main.async { self?.networkUp = path.status == .satisfied }
        }
        pathMonitor.start(queue: .global(qos: .utility))
    }

    private func assistLogin() {
        guard nativeAssignmentConfirmed else { return }
        let ready = autoLogin && !screenLocked && networkUp && Date() >= settledAfter
        let native = choices.native
        let nativeInfo = manager["native_status"] as? [String: Any] ?? [:]
        let needNative = native != nil && nativeInfo["phase"] as? String == "login-required"
        switch nativeGate.update(need: needNative, connected: native != nil && status.phase == "active" && status.profileID == native, ready: ready) {
        case .prompt?:
            openNativeLogin()
            notify("\(profiles[native!].name) 需要登录", "请在机构登录网页中按提示完成认证并唤起 aTrust。", action: "native")
        case .recovered?:
            notify("\(profiles[native!].name) 已重新连接", "本机分流已就绪。", action: nil)
        case nil: break
        }
        let remote = choices.container
        switch containerGate.update(need: remote != nil && isolated.school == remote && isolated.needsLogin,
                                    connected: remote != nil && isolated.school == remote && isolated.connected, ready: ready) {
        case .prompt?:
            openContainer()
            notify("\(profiles[remote!].name) 需要登录", "请在容器桌面内完成认证。", action: "container")
        case .recovered?:
            notify("\(profiles[remote!].name) 已重新连接", "业务连通检查已通过。", action: nil)
        case nil: break
        }
    }

    private func notify(_ title: String, _ body: String, action: String?) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        if let action { content.userInfo = ["login": action] }
        let request = UNNotificationRequest(identifier: "login-\(action ?? "ok")-\(title)", content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        DispatchQueue.main.async {
            switch response.notification.request.content.userInfo["login"] as? String {
            case "native": self.openNativeLogin()
            case "container": self.openContainer()
            default: break
            }
        }
        completionHandler()
    }

    func menuWillOpen(_ menu: NSMenu) { refresh(); menuOpen = true }
    func menuDidClose(_ menu: NSMenu) { menuOpen = false }

    private func item(_ text: String, _ selector: Selector? = nil) -> NSMenuItem {
        let value = NSMenuItem(title: text, action: selector, keyEquivalent: "")
        value.target = self
        if selector == nil { value.isEnabled = false }
        return value
    }

    /// A status row: coloured dot, name, short state. Clicking opens that login UI.
    private func line(_ name: String, _ summary: (String, NSColor), _ action: Selector?) -> NSMenuItem {
        let row = item("\(name)  \(summary.0)", action)
        row.isEnabled = true
        let config = NSImage.SymbolConfiguration(pointSize: 8, weight: .regular)
            .applying(.init(paletteColors: [summary.1]))
        row.image = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: summary.0)?
            .withSymbolConfiguration(config)
        return row
    }

    private var nativeAssignmentConfirmed: Bool {
        let age = Date().timeIntervalSince1970 - (manager["updated_at"] as? Double ?? 0)
        let native = manager["native_status"] as? [String: Any] ?? [:]
        return choices.valid && age >= 0 && age < 45 &&
            manager["revision"] as? Int64 == choices.revision &&
            ["ready", "applying"].contains(manager["phase"] as? String ?? "") &&
            manager["native"] as? String == choices.native &&
            manager["container"] as? String == choices.container &&
            native["school"] as? String == choices.native &&
            ["online", "login-required"].contains(native["phase"] as? String ?? "")
    }

    private func schoolSummary(_ school: String) -> (String, NSColor) {
        let selected = choices.enabled.contains(school)
        let age = Date().timeIntervalSince1970 - (manager["updated_at"] as? Double ?? 0)
        let fresh = age >= 0 && age < 45
        if working { return ("切换中…", .systemYellow) }
        if !fresh { return (selected ? "等待后台" : "未勾选", .tertiaryLabelColor) }
        if manager["phase"] as? String == "error" { return ("操作未完成", .systemRed) }
        if manager["phase"] as? String == "applying" { return ("切换中…", .systemYellow) }
        if manager["revision"] as? Int64 != choices.revision { return ("切换中…", .systemYellow) }
        // An unchecked item is not labelled disconnected until the manager has
        // acknowledged the new assignment.
        let actualNative = manager["native"] as? String
        let actualContainer = manager["container"] as? String
        if actualNative != choices.native || actualContainer != choices.container { return ("切换中…", .systemYellow) }
        if !selected { return ("未连接", .tertiaryLabelColor) }
        if choices.container == school {
            if !status.containerRouteError.isEmpty { return ("路由未就绪 · 容器", .systemRed) }
            guard isolated.school == school else { return ("准备容器…", .systemYellow) }
            return (isolated.summary.0 + " · 容器", isolated.summary.1)
        }
        let nativeInfo = manager["native_status"] as? [String: Any] ?? [:]
        if nativeInfo["phase"] as? String == "login-required" { return ("需要登录 · 本机", .systemOrange) }
        if status.profileID == school && status.enabled {
            return (status.summary.0 + " · 本机", status.summary.1)
        }
        return ("连接中 · 本机", .systemYellow)
    }

    private func refresh() {
        status = ServiceStatus(data: try? Data(contentsOf: statusURL))
        let settingsURL = connectionDirectory.appendingPathComponent("connections.json")
        let defaultChoices = Data("{\"version\":1,\"enabled\":[],\"native_preference\":\"a\"}".utf8)
        choices = ConnectionChoices(data: FileManager.default.fileExists(atPath: settingsURL.path)
            ? ((try? Data(contentsOf: settingsURL)) ?? Data()) : defaultChoices)
        isolated = ContainerStatus(data: try? Data(contentsOf: connectionDirectory.appendingPathComponent("status.json")))
        if let data = try? Data(contentsOf: connectionDirectory.appendingPathComponent("connections-status.json")),
           let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] { manager = json }
        if !smoke && profiles.configured && nativeQuitGate.update(choices: choices, manager: manager, service: status) {
            for app in NSWorkspace.shared.runningApplications where aTrustApps.contains(where: {
                $0.standardizedFileURL == app.bundleURL?.standardizedFileURL
            }) {
                // This installed aTrust handles Cocoa Quit by hiding windows.
                // Its own SIGTERM handler performs normal tray cleanup and
                // exits; never force-kill its agents or tunnel service.
                if !app.isTerminated { _ = Darwin.kill(app.processIdentifier, SIGTERM) }
            }
        }
        let assignment = [choices.native, choices.container]
        if assignment != loginAssignment {
            nativeGate = LoginGate()
            containerGate = LoginGate()
            loginAssignment = assignment
        }
        if !smoke && profiles.configured && choices.valid { assistLogin() }
        renderMenu()
    }

    private func renderMenu() {
        let icon = choices.enabled.isEmpty ? "power" : "arrow.triangle.branch"
        statusItem.button?.image = NSImage(systemSymbolName: icon, accessibilityDescription: "aTrust 分流")
        statusItem.button?.title = ""
        statusItem.button?.toolTip = LocalProfiles.ids.map { profiles[$0].name + "：" + schoolSummary($0).0 }.joined(separator: "\n")
        guard let menu = statusItem.menu else { return }
        menu.removeAllItems()
        menu.addItem(item("连接"))
        if !profiles.configured { menu.addItem(item("请先配置本地机构资料并安装")) }
        if !choices.valid { menu.addItem(item("连接选择无效或尚未创建")) }
        for school in LocalProfiles.ids {
            let row = line(profiles[school].name, schoolSummary(school), #selector(toggleSchool(_:)))
            row.representedObject = school
            row.state = choices.enabled.contains(school) ? .on : .off
            row.isEnabled = !working && profiles.configured && choices.valid
            menu.addItem(row)
        }
        menu.addItem(.separator())
        if let native = choices.native {
            menu.addItem(item("\(profiles[native].name) 网页登录", #selector(openNativeLogin)))
        }
        if let remote = choices.container {
            menu.addItem(item("打开 \(profiles[remote].name) 桌面", #selector(openContainer)))
        }
        if !choices.enabled.isEmpty { menu.addItem(item("重新检查", #selector(retryService))) }
        if manager["phase"] as? String == "error", let message = manager["message"] as? String {
            let detail = item("操作未完成 · 查看详情", #selector(showError))
            detail.toolTip = message
            menu.addItem(detail)
        }
        let settings = NSMenu()
        let location = NSMenu()
        for school in LocalProfiles.ids {
            let row = item(profiles[school].name, #selector(preferNative(_:)))
            row.representedObject = school
            row.state = choices.nativePreference == school ? .on : .off
            row.isEnabled = !working && choices.valid
            location.addItem(row)
        }
        let locationItem = item("双连接时本机运行")
        locationItem.isEnabled = true
        locationItem.submenu = location
        settings.addItem(locationItem)
        settings.addItem(item("只勾一个时，自动在本机运行"))
        settings.addItem(.separator())
        let assist = item("需要登录时提醒", #selector(toggleAutoLogin))
        assist.state = autoLogin ? .on : .off
        settings.addItem(assist)
        settings.addItem(item("打开配置文件", #selector(openConfiguration)))
        settings.addItem(.separator())
        if choices.container != nil && isolated.memoryMB > 0 {
            settings.addItem(item("容器内存 \(isolated.memoryMB) MiB（含缓存）"))
        }
        settings.addItem(item("查看日志", #selector(openLogs)))
        settings.addItem(item("使用说明", #selector(openHelp)))
        settings.addItem(item("安装或修复…", #selector(installService)))
        settings.addItem(item("卸载…", #selector(uninstallService)))
        let settingsItem = item("设置")
        settingsItem.isEnabled = true
        settingsItem.submenu = settings
        menu.addItem(settingsItem)
        menu.addItem(.separator())
        menu.addItem(item("退出菜单栏", #selector(quit)))
    }

    /// Constructs the real menu with synthetic data only. No launch lifecycle,
    /// helpers, notifications, timers, filesystem configuration or network I/O.
    func runUISmoke() {
        smoke = true
        profiles = LocalProfiles(data: Self.exampleProfileData())
        profiles.configured = true // Synthetic installed state; no menu actions are invoked.
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.menu = NSMenu()
        let now = Date().timeIntervalSince1970
        func jsonValue(_ value: String?) -> Any { if let value { return value }; return NSNull() }
        for enabled in [[], ["a"], ["b"], ["a", "b"]] {
            for preference in LocalProfiles.ids {
                choices = ConnectionChoices(data: try! JSONSerialization.data(withJSONObject: [
                    "version": 1, "enabled": enabled, "native_preference": preference, "revision": 1]))
                manager = ["phase": "ready", "updated_at": now, "revision": choices.revision,
                           "native": jsonValue(choices.native),
                           "container": jsonValue(choices.container),
                           "native_status": ["phase": choices.native == nil ? "stopped" : "login-required"]]
                status = ServiceStatus(data: try! JSONSerialization.data(withJSONObject: [
                    "phase": choices.native == nil ? "paused" : "logged-out",
                    "enabled": choices.native != nil, "profile_id": jsonValue(choices.native),
                    "short_name": jsonValue(choices.native.map { profiles[$0].shortName }),
                    "updated_at": now]))
                isolated = ContainerStatus(data: try! JSONSerialization.data(withJSONObject: [
                    "school": choices.container ?? "a", "phase": choices.container == nil ? "stopped" : "login-required",
                    "enabled": choices.container != nil, "updated_at": now]))
                renderMenu()
                let rows = statusItem.menu!.items.filter { $0.representedObject is String }
                precondition(rows.count == 2)
                for row in rows {
                    let id = row.representedObject as! String
                    precondition(row.state == (choices.enabled.contains(id) ? .on : .off))
                    precondition(row.title.hasPrefix(profiles[id].name))
                }
                print("UI selection=\(enabled) preference=\(preference)")
                printMenu(statusItem.menu!)
            }
        }
        NSStatusBar.system.removeStatusItem(statusItem)
        print("UI smoke: PASS (synthetic menu only)")
    }

    private func printMenu(_ menu: NSMenu, depth: Int = 0) {
        for row in menu.items where !row.isSeparatorItem {
            print(String(repeating: "  ", count: depth) + (row.state == .on ? "✓ " : "") + row.title)
            if let submenu = row.submenu { printMenu(submenu, depth: depth + 1) }
        }
    }

    static func exampleProfileData() -> Data {
        try! JSONSerialization.data(withJSONObject: ["version": 1, "profiles": LocalProfiles.ids.map { id in
            ["id": id, "name": "机构 \(id.uppercased())", "short_name": "显示 \(id.uppercased())",
             "domains": ["\(id).example.org"], "gateways": ["vpn.\(id).example.org"],
             "login_url": "https://vpn.\(id).example.org/custom/login/"] as [String: Any]
        }])
    }

    private func command(_ arguments: [String]) {
        guard !smoke, let base = Bundle.main.resourceURL else { return }
        let executable = base.appendingPathComponent("runtime/atrust-splitd")
        working = true
        DispatchQueue.global(qos: .userInitiated).async {
            let task = Process()
            task.executableURL = executable
            task.arguments = arguments
            let output = Pipe()
            task.standardOutput = output
            task.standardError = output
            var failure: String? = nil
            do {
                try task.run()
                let result = output.fileHandleForReading.readDataToEndOfFile()
                task.waitUntilExit()
                if task.terminationStatus != 0 { failure = String(data: result, encoding: .utf8) }
            } catch { failure = error.localizedDescription }
            DispatchQueue.main.async {
                self.working = false
                if let failure {
                    let alert = NSAlert()
                    alert.messageText = "操作未完成"
                    alert.informativeText = String(failure.suffix(500)) + "\n可从“更多”中选择“安装或修复”。"
                    alert.runModal()
                }
                self.refresh()
            }
        }
    }

    @objc private func toggleSchool(_ sender: NSMenuItem) {
        guard let school = sender.representedObject as? String else { return }
        command(["school", "--school", school, "--enabled", choices.enabled.contains(school) ? "no" : "yes"])
    }
    @objc private func preferNative(_ sender: NSMenuItem) {
        guard let school = sender.representedObject as? String else { return }
        command(["prefer-native", "--school", school])
    }
    @objc private func retryService() {
        if status.fresh { command(["retry"]) }
        if isolated.fresh && choices.container != nil { command(["dual-check"]) }
    }
    @objc private func showError() {
        let alert = NSAlert()
        alert.messageText = "连接切换未完成"
        alert.informativeText = manager["message"] as? String ?? "请查看日志。"
        alert.runModal()
    }
    @objc private func openConfiguration() {
        NSWorkspace.shared.open(URL(fileURLWithPath: LocalProfiles.path))
    }
    @objc private func openNativeLogin() {
        // Always enter through the selected school's official portal. Opening
        // the client first would show its remembered, possibly different school.
        guard !smoke, profiles.configured, nativeAssignmentConfirmed,
              let url = choices.nativeLoginURL(profiles: profiles) else { return }
        NSWorkspace.shared.open(url)
    }
    @objc private func toggleAutoLogin() { autoLogin.toggle(); refresh() }
    @objc private func openContainer() {
        guard !smoke, profiles.configured, nativeAssignmentConfirmed, let school = choices.container else { return }
        if isolated.needsLogin && isolated.school == school { command(["container-login", "--school", school]) }
        let port = school == "a" ? 18081 : 18082
        var components = URLComponents(string: "http://127.0.0.1:\(port)/")!
        var fragment = URLComponents()
        var settings = [URLQueryItem(name: "autoconnect", value: "true"), URLQueryItem(name: "resize", value: "scale")]
        let passwordFile = "vnc-password-\(school)"
        if let secret = try? String(contentsOf: connectionDirectory.appendingPathComponent(passwordFile), encoding: .utf8) {
            settings.append(URLQueryItem(name: "password", value: secret.trimmingCharacters(in: .whitespacesAndNewlines)))
        }
        fragment.queryItems = settings
        components.percentEncodedFragment = fragment.percentEncodedQuery
        if let url = components.url { NSWorkspace.shared.open(url) }
    }
    @objc private func openLogs() {
        NSWorkspace.shared.open(URL(fileURLWithPath: "/Library/Logs/ATrustSplit"))
    }
    @objc private func openHelp() {
        if let url = Bundle.main.resourceURL?.appendingPathComponent("使用说明.txt") { NSWorkspace.shared.open(url) }
    }
    @objc private func installService() {
        if let url = Bundle.main.resourceURL?.appendingPathComponent("install.command") { NSWorkspace.shared.open(url) }
    }
    @objc private func uninstallService() {
        if let url = Bundle.main.resourceURL?.appendingPathComponent("uninstall.command") { NSWorkspace.shared.open(url) }
    }
    @objc private func quit() { NSApp.terminate(nil) }
}

@main
struct Entry {
    static func main() {
        if CommandLine.arguments.contains("--self-test") {
            func data(_ object: [String: Any]) -> Data { try! JSONSerialization.data(withJSONObject: object) }
            let profiles = LocalProfiles(data: AppDelegate.exampleProfileData())
            precondition(profiles["a"].name == "机构 A" && profiles["b"].shortName == "显示 B")
            precondition(!profiles.configured && !LocalProfiles().configured)
            let active = ServiceStatus(data: data([
                "phase": "active", "message": "分流运行中", "short_name": "显示 A", "profile_id": "a",
                "updated_at": Date().timeIntervalSince1970, "selection": "auto", "enabled": true]))
            precondition(active.title == "分流运行中 · 显示 A" && active.fresh && active.profileID == "a")
            precondition(ServiceStatus().phase == "not-installed")
            let connectedData = data(["phase": "connected", "updated_at": Date().timeIntervalSince1970])
            precondition(ContainerStatus(data: connectedData).connected)
            precondition(ServiceStatus(data: data([
                "phase": "logged-out", "enabled": true, "updated_at": Date().timeIntervalSince1970])).needsLogin)
            var gate = LoginGate()
            let t0 = Date()
            precondition(gate.update(need: true, connected: false, ready: true, now: t0) == nil)
            precondition(gate.update(need: true, connected: false, ready: false, now: t0 + 20) == nil)
            precondition(gate.update(need: true, connected: false, ready: true, now: t0 + 20) == .prompt)
            precondition(gate.update(need: true, connected: false, ready: true, now: t0 + 40) == nil)
            precondition(gate.update(need: false, connected: true, ready: true, now: t0 + 50) == .recovered)
            precondition(gate.update(need: false, connected: true, ready: true, now: t0 + 60) == nil)
            precondition(gate.update(need: true, connected: false, ready: true, now: t0 + 70) == nil)
            precondition(active.summary.0 == "已连接" && ContainerStatus(data: connectedData).summary.0 == "已连接")
            precondition(ContainerStatus().summary.0 == "未安装")
            precondition(ContainerStatus(data: data([
                "phase": "connected", "memory_mb": 900, "updated_at": Date().timeIntervalSince1970])).memoryMB == 900)
            for enabled in [[], ["a"], ["b"], ["a", "b"]] {
                for preferred in LocalProfiles.ids {
                    let choices = ConnectionChoices(data: data(["version": 1, "enabled": enabled, "native_preference": preferred]))
                    precondition(choices.valid)
                    precondition(choices.container == nil || choices.container != choices.native)
                    precondition((choices.native == nil) == enabled.isEmpty)
                    if enabled.count == 1 { precondition(choices.native == enabled.first && choices.container == nil) }
                    if enabled.count == 2 { precondition(choices.native == preferred && choices.container != preferred) }
                }
            }
            for invalid in [
                ["version": 1, "enabled": ["a", "a"], "native_preference": "a"],
                ["version": 1, "enabled": ["unknown"], "native_preference": "a"],
                ["version": true, "enabled": ["a"], "native_preference": "a"],
                ["version": 1, "enabled": ["a"], "native_preference": "b", "revision": -1]
            ] as [[String: Any]] {
                let choices = ConnectionChoices(data: data(invalid))
                precondition(!choices.valid && choices.enabled.isEmpty)
            }
            let stoppedChoices = ConnectionChoices(data: data([
                "version": 1, "enabled": [], "native_preference": "a", "revision": Int64(1793782940168013000)]))
            let stoppedService = ServiceStatus(data: data([
                "phase": "paused", "enabled": false, "managed_routes": 0, "routes_restored": true,
                "assignment": ["native": NSNull(), "container": NSNull(), "revision": stoppedChoices.revision],
                "updated_at": t0.timeIntervalSince1970]))
            var stoppedManager: [String: Any] = ["phase": "ready", "native": NSNull(), "container": NSNull(),
                "native_status": ["phase": "stopped"], "updated_at": t0.timeIntervalSince1970,
                "revision": stoppedChoices.revision]
            var quitGate = NativeQuitGate()
            precondition(!quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: active, now: t0))
            stoppedManager["revision"] = stoppedChoices.revision - 1
            precondition(!quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: stoppedService, now: t0))
            stoppedManager["revision"] = stoppedChoices.revision
            precondition(!quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: stoppedService, now: t0 + 60))
            stoppedManager["phase"] = "error"
            precondition(!quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: stoppedService, now: t0))
            stoppedManager["phase"] = "ready"
            var unconfirmedRestore = stoppedService
            unconfirmedRestore.routesRestored = false
            precondition(!quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: unconfirmedRestore, now: t0))
            unconfirmedRestore.routesRestored = true
            unconfirmedRestore.assignment["revision"] = stoppedChoices.revision - 1
            precondition(!quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: unconfirmedRestore, now: t0))
            precondition(quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: stoppedService, now: t0))
            // A manually reopened official app is not repeatedly closed.
            precondition(!quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: stoppedService, now: t0 + 1))
            // Invalid/missing choices must not rearm the quit gate.
            precondition(!quitGate.update(choices: ConnectionChoices(), manager: stoppedManager, service: stoppedService, now: t0))
            precondition(!quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: stoppedService, now: t0))
            let selected = ConnectionChoices(data: data(["version": 1, "enabled": ["b"], "native_preference": "a"]))
            precondition(!quitGate.update(choices: selected, manager: stoppedManager, service: stoppedService, now: t0))
            precondition(quitGate.update(choices: stoppedChoices, manager: stoppedManager, service: stoppedService, now: t0))
            precondition(stoppedChoices.nativeLoginURL(profiles: profiles) == nil)
            precondition(selected.nativeLoginURL(profiles: profiles)?.absoluteString == "https://vpn.b.example.org/custom/login/")
            print("Native status, connection selection and safe quit models: PASS")
            return
        }
        let app = NSApplication.shared
        let delegate = AppDelegate()
        if CommandLine.arguments.contains("--ui-smoke") {
            delegate.runUISmoke()
            return
        }
        app.delegate = delegate
        app.run()
    }
}
