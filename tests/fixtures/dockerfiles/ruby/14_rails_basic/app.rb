require 'sinatra'
require 'json'

set :bind, '0.0.0.0'
set :port, 3000

get '/' do
  content_type :json
  { message: 'Hello from Ruby!' }.to_json
end

get '/health' do
  content_type :json
  { status: 'healthy' }.to_json
end
