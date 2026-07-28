# Redmine を検証用に設定する。
#
#   docker cp verify/redmine-setup.rb kb-redmine:/tmp/setup.rb
#   docker exec kb-redmine bundle exec rails runner /tmp/setup.rb -e production
#
# 行うこと: REST API の有効化、管理者の API キー確定、検証用プロジェクト作成、
# ラベル用カスタムフィールドの作成。

Setting.rest_api_enabled = '1'

admin = User.find_by_login('admin')
admin.password = 'DevPass!2026'
admin.password_confirmation = 'DevPass!2026'
admin.must_change_passwd = false
admin.save!
key = admin.api_key

if Tracker.count.zero? || IssueStatus.count.zero? || IssuePriority.count.zero?
  Redmine::DefaultData::Loader.load('en')
end

project = Project.find_by_identifier('kb-adapter-test') ||
          Project.create!(name: 'KB Adapter Test', identifier: 'kb-adapter-test', is_public: false)
project.enabled_module_names = ['issue_tracking']
project.trackers = Tracker.all
project.save!

# Redmine にラベル機能は無い。最も近いのは複数選択のカスタムフィールド。
field = IssueCustomField.find_by(name: 'Labels') || IssueCustomField.create!(
  name: 'Labels',
  field_format: 'list',
  possible_values: %w[bug db urgent kb-test kb-second],
  multiple: true,
  is_filter: true,
  searchable: true
)
field.trackers = Tracker.all
field.projects = Project.all
field.save!

puts "KB_REDMINE_API_KEY=#{key}"
puts "KB_REDMINE_PROJECT=#{project.identifier}"
puts "LABEL_FIELD=#{field.name} (id=#{field.id})"
