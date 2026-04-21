const path = require('path');
const HtmlWebpackPlugin = require('html-webpack-plugin');
const CopyPlugin = require('copy-webpack-plugin');

module.exports = {
  entry: './src/index.tsx',
  output: {
    path: path.resolve(__dirname, 'dist'),
    filename: 'bundle.[contenthash].js',
    clean: true,
    publicPath: '/',
  },
  resolve: {
    extensions: ['.ts', '.tsx', '.js', '.jsx'],
  },
  module: {
    rules: [
      {
        test: /\.tsx?$/,
        use: 'ts-loader',
        exclude: /node_modules/,
      },
      {
        test: /\.css$/,
        use: ['style-loader', 'css-loader'],
      },
    ],
  },
  plugins: [
    new HtmlWebpackPlugin({
      template: './public/index.html',
    }),
    // Ship the AudioWorklet processor alongside the bundle. HtmlWebpackPlugin
    // only emits index.html, so without this the worklet 404s at runtime.
    // config.js is excluded — it's written by CDK / setup-agentcore.
    new CopyPlugin({
      patterns: [
        {
          from: path.resolve(__dirname, 'public'),
          to: path.resolve(__dirname, 'dist'),
          globOptions: { ignore: ['**/index.html', '**/config.js'] },
          noErrorOnMissing: true,
        },
      ],
    }),
  ],
  devServer: {
    port: 3000,
    hot: true,
    historyApiFallback: true,
    open: false,
  },
};
